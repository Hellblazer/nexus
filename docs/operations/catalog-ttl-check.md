# Catalog TTL CHECK runbook (RDR-236, nexus-6pfgb.44)

A note's own `ttl_days` lives in its catalog entry (`nexus.catalog_documents.metadata`), and the clock it counts from is `metadata.put_at`. This runbook adds a table CHECK that says: **an entry that carries a TTL must carry a non-empty `put_at`.** It is an operator step with three engine routes. Nothing calls them; no startup, upgrade rung, hook, doctor row or schedule does.

## Why it is a step of its own

- **Not a Liquibase changeset.** A boot-time VALIDATE crash-loops an install whose rows differ from the measured one.
- **Not the chunk-table routes** (`/v1/admin/chunk-keys/*`, nexus-6pfgb.43). Those carry `nexus.chunks` constraints only.
- **After the Phase 4 Step 2c paired cut.** A client older than nexus-6pfgb.15 writes `ttl_days` without `put_at`, and a NOT VALID CHECK refuses its note write. After 2c that client is already refused with 422, so the add changes nothing on the wire.
- **After the backfill.** A NOT VALID CHECK is evaluated on every row version written, an UPDATE that touches no metadata included. An entry with a TTL and no `put_at` could then not be updated or tombstoned (`nx store expire` would fail on it) until it is stamped. The add route therefore refuses while any such entry exists.

## The routes

All `POST`, root credential only, under `/v1/admin/catalog-ttl-check`. The body is ignored.

| Route | What it does |
|---|---|
| `/census` | Read only. Per tenant: `rows`, `ttl_rows` (entries that carry a TTL), `violating_rows` (a TTL and no `put_at`), `violating_tombstoned_rows`; the total, the tenants checked, the table rows examined, and the constraint's state. |
| `/add` | Adds `catalog_documents_ttl_put_at_check` NOT VALID. 409 `tenant_violates_constraint` naming each tenant while any violator exists. A rerun answers `present`. |
| `/validate` | Reads `pg_constraint.convalidated` first. `already_valid` and `absent` answer at once, with no count. Otherwise counts every tenant and answers 409 `tenant_violates_constraint` naming each tenant with a violator, else runs `VALIDATE CONSTRAINT`. |

**Order:** census, backfill until the census reads zero, add, census again, validate. Validate changes no data (it takes SHARE UPDATE EXCLUSIVE, which lets writers through). A violator that appears after the add is impossible: the NOT VALID CHECK refuses new violators.

### The count is every tenant's

`nexus.catalog_documents` is FORCE row-level-security, keyed to the tenant stamp. A role that is neither superuser nor BYPASSRLS reads zero rows through it, and a count of zero would look clean. So the routes:

1. read the census on the admin session (`NX_DB_ADMIN_*`), which sees tenants with no `service_tokens` row, and refuse with 503 `admin_cannot_see_all_tenants` unless that role is a superuser or BYPASSRLS (the table has no policy that lets an owner read every row, unlike `nexus.chunks`);
2. take a tenant-scoped row count of the default tenant and of every tenant with a token first, and refuse with 503 `admin_probe_undercount` if the census is below it for any of them.

Even a census that missed a tenant could not validate a table that holds a violator: `VALIDATE CONSTRAINT` reads the table without the policy and fails with SQLSTATE 23514 (409 `check_violation`), leaving the constraint NOT VALID.

## What counts as a TTL

The CHECK and the census use one predicate, written once in `CatalogTtlCheckAdmin`. It matches what `nexus.catalog.note_ttl.note_ttl_days` reads as more than zero:

| `metadata.ttl_days` | Carries a TTL? |
|---|---|
| a JSON number from 1 up to 999999999 (7, 7.5) | yes |
| 0, 0.5, a negative number, 1000000000 or more, 1e400 | no (the reader treats them as permanent) |
| **a string of ASCII digits** (`"7"`, `" 7 "`, `"+7"`, `"007"`) with a value in 1..999999999 | **yes**: `int("7")` is 7 and the expiry sweep acts on it |
| any other string (`"abc"`, `""`, `"7.0"`, `"-7"`), a boolean, null, an object, an array, an absent key | no |
| `"1_0"`, or a non-ASCII decimal digit | no, deliberately: Python's `int()` reads them, the CHECK does not. Such an entry falls back to `indexed_at` in `clock_start`, which expires it late and never early. No writer produces a string TTL |

`put_at` is present when it is a non-empty JSON string. `""` means absent: a retraction writes `ttl_days: 0` with `put_at: ""`, and that pair passes. A number, boolean, null or object is not a stamp. A string that is not an ISO instant passes (SQL cannot mirror `parse_iso`). A NULL `metadata`, or one that is not an object, passes.

The expression is cast-safe. Postgres does not promise to evaluate AND in the order written, so every cast sits inside a `CASE` branch chosen by `jsonb_typeof`, and the string branch has no cast at all (a regular expression bounds the digits). `put_at` is coalesced, because a NULL from a missing key would let the row through.

`service/src/test/resources/catalog-ttl-check-shapes.json` is the corpus: the engine test holds the predicate to it on PostgreSQL, and `tests/test_catalog_ttl_check_shapes.py` holds `note_ttl_days` to it.

## Not wired, and not yet run

No client verb sends these routes yet, and the add waits for Sam's Step 2c enforce cut. The census can run at any time and changes nothing.
