# Manifest chunk-index CHECK runbook (RDR-236, nexus-6pfgb.42)

Every row of `nexus.catalog_document_chunks` carries its place in the document twice: `position` (the manifest key) and `chunk_index` (the chunk's own index). They are equal by construction: `chunk_index` is NOT NULL since hygiene-001, and a BEFORE INSERT trigger (`trg_catalog_document_chunks_chunk_index_default`) fills it from `position` when a writer omits it. No current writer sets the two apart. This runbook adds a table CHECK, `catalog_document_chunks_chunk_index_position_check`, that says **`chunk_index = position`**, so a later writer cannot break it. It is an operator step with three engine routes. Nothing calls them; no startup, upgrade rung, hook, doctor row or schedule does.

It is the same shape as the [catalog TTL CHECK](catalog-ttl-check.md) and shares its admin-connection machinery (`AdminDdlSession`). Read that runbook for the details this one only names.

## Why it is a step of its own

- **Not a Liquibase changeset.** A boot-time VALIDATE crash-loops an install whose rows differ from the measured one, and a release does not rewrite user data at engine boot.
- **A NOT VALID CHECK is evaluated on every row version written**, an UPDATE that touches no column of the check included. A manifest row that breaks it could not be updated until `chunk_index` is set back to `position`. The add route therefore refuses while any such row exists.
- **Out of scope:** the `labels_written` CHECK and any manifest-collection constraint are not part of this step.

## The routes

All `POST`, root credential only, under `/v1/admin/manifest-chunk-index-check`. The body is ignored.

| Route | What it does |
|---|---|
| `/census` | Read only. Per tenant: `rows` and `violating_rows` (`chunk_index <> position`); the total, the tenants checked, the table rows examined, and the constraint's state. |
| `/add` | Reads the constraint's state first: a constraint that exists answers `present` at once, with no count. Otherwise counts every tenant and answers 409 `tenant_violates_constraint` naming each tenant with a violator, else adds the CHECK NOT VALID, then counts again and reports `violating_after_add`. |
| `/validate` | Reads `pg_constraint.convalidated` first. `already_valid` and `absent` answer at once, with no count. Otherwise counts every tenant and answers 409 `tenant_violates_constraint` naming each tenant with a violator, else runs `VALIDATE CONSTRAINT`. |

**Order:** census (expected to read zero), add, read `violating_after_add` in its answer, census again, validate. Validate changes no data (SHARE UPDATE EXCLUSIVE, which lets writers through). If the census names a violator, set `chunk_index` to `position` on those rows first; no route does it.

**Synchronous.** The census (up to 10 minutes, 60 seconds as the table owner) and validate (up to 30 minutes) answer when they finish. The public edge gives up on a request after about 30 seconds; the engine carries on. A retry of add or validate during its constraint statement answers 409 `constraint_run_in_progress`; call the census to see where it stands.

### The count is every tenant's

`nexus.catalog_document_chunks` is FORCE row-level-security, keyed to the tenant stamp, with no policy that lets its owner read every row. The census reads on the admin session (`NX_DB_ADMIN_*`) one of three ways, exactly as the TTL step does: a superuser or BYPASSRLS role reads the table as it is; the table's owner (the managed service's `nexus_admin`) reads with FORCE lifted for one transaction that is always rolled back, under an ACCESS EXCLUSIVE lock on the manifest for the scan (5 seconds to get it, 60 seconds for the scan, refused during a migration); any other role is 503 `admin_cannot_see_all_tenants`. A tenant with no `service_tokens` row is counted. As a second line the routes compare the census with a tenant-scoped row count and refuse with 503 `admin_probe_undercount` if it is below it. The manifest is the largest catalog table: run the census, and an add or validate that has to count, in a quiet window.

Add needs ownership only (no scan); validate needs ownership and no row visibility; both count first unless the constraint is already there.

## Not wired, and not yet run

No client verb sends these routes. The census changes no data, but as the table owner it holds an ACCESS EXCLUSIVE lock on the manifest while it scans.
