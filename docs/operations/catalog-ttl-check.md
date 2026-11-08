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
| `/add` | Reads the constraint's state first: a constraint that exists answers `present` at once, with no count. Otherwise counts every tenant and answers 409 `tenant_violates_constraint` naming each tenant with a violator, else adds `catalog_documents_ttl_put_at_check` NOT VALID, then counts again and reports `violating_after_add`. |
| `/validate` | Reads `pg_constraint.convalidated` first. `already_valid` and `absent` answer at once, with no count. Otherwise counts every tenant and answers 409 `tenant_violates_constraint` naming each tenant with a violator, else runs `VALIDATE CONSTRAINT`. |

**Order:** census, backfill until the census reads zero (the exceptions are named under "Tombstoned violators, and other residue the backfill cannot reach", below), add, read `violating_after_add` in its answer, census again, validate. Validate changes no data (it takes SHARE UPDATE EXCLUSIVE, which lets writers through).

**The count and the add are two statements with no lock between them.** A legacy client that writes a TTL with no `put_at` in that window leaves an entry the new CHECK then freezes (it cannot be updated or tombstoned). The add therefore counts again after the ADD and answers `violating_after_add` with the tenants (`violators_after_add`) and a `warning` when it is not zero. The CHECK refuses every later such write, so that second count is final. Stamp the named entries (the backfill, or the remedies in the tombstoned-violators section below), run the census, then validate. If the second count itself cannot be taken, `violating_after_add` is null and the `warning` says so; the constraint is in place either way.

**The routes are synchronous.** The census (up to 10 minutes, 60 seconds as the table owner) and validate (up to 30 minutes) answer when they finish. The public edge gives up on a request after about 30 seconds and the client sees a timeout; the engine carries on and finishes the run. Add and validate count first and take the run lock only for the constraint statement itself, so 409 `constraint_run_in_progress` answers a retry only once that statement is running (the census holds no lock). A retry during the count runs a second count: as a superuser or BYPASSRLS role it runs alongside the first; as the table owner it waits behind the first count's table lock and answers 503 `lock_timeout` after 5 seconds. Wait, then call the census to see where it stands: an add that finished answers `present`, a validate that finished answers `already_valid`.

### The count is every tenant's

`nexus.catalog_documents` is FORCE row-level-security, keyed to the tenant stamp. A role that is neither superuser nor BYPASSRLS reads zero rows through it, and a count of zero would look clean. The table has no policy that lets its owner read every row (unlike `nexus.chunks`), so the routes read the census one of three ways, on the admin session (`NX_DB_ADMIN_*`), which also sees tenants with no `service_tokens` row:

1. **A superuser or BYPASSRLS role** reads the table as it is. No lock.
2. **The table's owner** (the managed service's `nexus_admin`: schema owner, NOSUPERUSER, not BYPASSRLS) reads with FORCE row-level security lifted for one transaction that is always rolled back (`ALTER TABLE ... NO FORCE ROW LEVEL SECURITY` is transactional, so the rollback puts FORCE back whatever fails; the same step the aspect-queue drain takes). **No Liquibase policy is added.** The cost is real: that ALTER takes an ACCESS EXCLUSIVE lock, so every read and write of the catalog waits behind the scan. While the ALTER itself waits for the lock (up to 5 seconds, behind any long reader), it queues too: every new catalog access lines up behind it, and the engine request threads blocked there hold their pool connections. It is bounded: the lock is waited for 5 seconds (503 `lock_timeout`, nothing changed), the scan is stopped at 60 seconds (503 `read_timeout`, nothing changed), and a schema migration in progress refuses it (503 `migration_in_progress`). Run the census, and an add or validate that has to count, in a quiet window.
3. **Any other role** is refused with 503 `admin_cannot_see_all_tenants`.

What each step needs of the role: the census, the view above; **add**, ownership for the ADD itself (it scans no row), **validate**, ownership and no row visibility for the `VALIDATE CONSTRAINT` itself, which reads the table as the constraint check, not through the policy. But the routes count first (add while the constraint is absent, validate while it is NOT VALID), so those two calls need the all-tenant view as well; only a rerun that finds the constraint already there (`present`, `already_valid`, `absent`) needs neither. So even a count that missed a tenant could not validate a table that holds a violator: it fails with SQLSTATE 23514 (409 `check_violation`) and the constraint stays NOT VALID. The integration test runs add and validate as a non-superuser owner under FORCE, and pins that failure on a violator the owner cannot read.

As a second line the routes take a tenant-scoped row count of the default tenant and of every tenant with a token first, and refuse with 503 `admin_probe_undercount` if the census is below it for any of them.

## What counts as a TTL

The CHECK and the census use one predicate, written once in `CatalogTtlCheckAdmin`. It matches what `nexus.catalog.note_ttl.note_ttl_days` reads as more than zero, **with the named gaps below**; it does not match it exactly.

| `metadata.ttl_days` | Carries a TTL? |
|---|---|
| a JSON number from 1 up to 999999999 (7, 7.5) | yes |
| 0, 0.5, a negative number, 1000000000 or more, 1e400 | no (the reader treats them as permanent) |
| **a string of ASCII digits** (`"7"`, `" 7 "`, `"\t7\n"`, `"+7"`, `"007"`) with a value in 1..999999999 | **yes**: `int("7")` is 7 and the expiry sweep acts on it |
| any other string (`"abc"`, `""`, `"7.0"`, `"-7"`), a boolean, null, an object, an array, an absent key | no |

**The named gaps.** These are the shapes where the reader and the CHECK differ. The shape corpus (`service/src/test/resources/catalog-ttl-check-shapes.json`) names each under `differs_from_reader`, and no writer produces any of them (the put path writes integers); they are hand edits and imports.

- **Not counted, though the reader expires on them** (fail safe: such an entry can never be frozen, and `clock_start` falls back to `indexed_at`, which expires it late and never early):
  a digit string with underscores (`"1_0"`); a non-ASCII decimal digit (Arabic-Indic, full-width `"７"`); whitespace around the digits that Python strips and the pattern does not (a no-break space; `\x1c` to `\x1f` and `\x85`; the pattern's `\s` is ASCII whitespace and, past that, the database locale's); a JSON number that Python rounds up to 1.0 while the stored value is below 1 (`0.99999999999999999999`).
- **Counted, though the reader calls them permanent** (stricter: the census names the entry and the add waits for a stamp, so nothing is frozen and nothing expires early):
  a JSON number that Python rounds past `timedelta.max.days` (`999999999.9999999999999`).

`put_at` is present when it is a non-empty JSON string. `""` means absent: a retraction writes `ttl_days: 0` with `put_at: ""`, and that pair passes. A number, boolean, null or object is not a stamp. A string that is not an ISO instant passes (SQL cannot mirror `parse_iso`). A NULL `metadata`, or one that is not an object, passes (a JSON string holding JSON text is one of those).

The expression is cast-safe. Postgres does not promise to evaluate AND in the order written, so every cast sits inside a `CASE` branch chosen by `jsonb_typeof`, and the string branch has no cast at all (a regular expression bounds the digits). `put_at` is coalesced, because a NULL from a missing key would let the row through.

`service/src/test/resources/catalog-ttl-check-shapes.json` is the corpus: the engine test holds the predicate to it on PostgreSQL, and `tests/test_catalog_ttl_check_shapes.py` holds `note_ttl_days` to it, including the named gaps.

## Tombstoned violators, and other residue the backfill cannot reach

The census counts tombstoned entries (`violating_tombstoned_rows`), and add and validate refuse on them, because a NOT VALID CHECK is evaluated on a tombstone's rows too. The 6pfgb.10 backfill (`nx catalog backfill-meta`) lists and updates **live** documents only: the engine's catalog update refuses a tombstone by design, so the backfill cannot stamp them and "backfill until the census reads zero" would never end. Notes that expired in the window between nexus-6pfgb.5 and nexus-6pfgb.15 are exactly this population.

Tombstones are not the only residue. The backfill also leaves **live** violators it cannot settle: its outcomes `residue:no_row` (no chunk row to take a clock from), `shared` (the chunk rows are not this note's own) and `residue:row_ttl_differs` (the row it would take the clock from carries a different TTL than the entry). And one shape the census counts and the backfill does not treat as open: a number that Python rounds past `timedelta.max.days` (`999999999.9999999999999`) with `put_at` `""`, because the backfill reads it as permanent (no TTL, so `""` is the retraction) while the SQL predicate counts it. For all of these the census cannot reach zero by backfilling; stamp them by hand at the live step or leave them to a bead, and add only once the census names none.

Two remedies exist, and neither is built here. One stamps `put_at` on each tombstoned violator with the entry's own `indexed_at`, which is what `clock_start` already falls back to, so a tombstone that is later restored expires exactly when it would have. Where `indexed_at` is empty that does not hold: `clock_start` returns `""` for it and the note never lapses, so stamping `deleted_at` instead makes it lapse at `deleted_at` plus its TTL, which is a change of behaviour, not a preservation of it. It needs a write that lifts FORCE for one owner transaction, since the engine's catalog update refuses a tombstone. The other deletes the tombstones (`purge-trash`), which is Sam's call, tenant-wide. A third path needs no action: tombstone GC ages the tombstones out and removes the violators with them. Note that restoring a tombstoned violator is itself refused once the CHECK exists (NOT VALID still evaluates it on the new row version), so a restore must come after the stamp. Which one, if either, is decided at the live step from the census's `violating_tombstoned_rows`: the count may well be zero, and no SQL is given here because an untested operator statement on a live catalog table is worse than none (a tested one would need a new SQL-text JDBC entry, which `SqlStringLiteralGateTest` admits only on Sam's acceptance).

## Not wired, and not yet run

No client verb sends these routes yet, and the add waits for Sam's Step 2c enforce cut. The census changes no data, but it is not free: as a superuser or BYPASSRLS role it takes no lock, while as the table owner it holds an ACCESS EXCLUSIVE lock on the catalog for up to 60 seconds. Run it in a quiet window.
