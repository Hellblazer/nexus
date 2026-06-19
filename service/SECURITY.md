# Security Notes — nexus-service (RDR-152)

## Bootstrap Posture (Phase 1-4)

A single shared `NX_SERVICE_TOKEN` authenticates **all** clients. Any token holder
can claim any tenant by setting the `X-Nexus-Tenant` header. The DB layer enforces
Row-Level Security (RLS with `FORCE`), but it trusts the client to name the correct
tenant. There is no per-tenant or per-session credential isolation at the HTTP layer.

**Do NOT deploy in shared or multi-principal environments before bead nexus-gmiaf.32
ships.** In single-operator environments (local dev, single-admin daemon) the bootstrap
posture is acceptable — the threat model is a local process, not an untrusted network.

Per-tenant credentials, token rotation, and a full auth lifecycle land in Phase 5
(bead nexus-gmiaf.32).

## GUC / Pooler Constraint

`SET LOCAL` (GUC `is_local=true`) is transaction-scoped and safe under a
**transaction-mode** pooler. A session-mode pooler (e.g. PgBouncer in its default
mode) would leak the `nexus.tenant` GUC stamp to the next connection borrower.
v1 connects directly to local PostgreSQL with no interposing pooler. If PgBouncer or
an equivalent is ever added it **must** be configured in transaction mode.

## Constant-Time Token Comparison

`AuthFilter` uses `MessageDigest.isEqual` for constant-time byte comparison.
Do not replace it with `String.equals` — the latter is subject to short-circuit
timing attacks.
