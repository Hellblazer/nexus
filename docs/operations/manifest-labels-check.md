# Manifest labels CHECK runbook (RDR-236, nexus-6pfgb.42)

A manifest row of `nexus.catalog_document_chunks` carries three per-position labels (`section_title`, `section_type`, `page_number`, all nullable) and a marker, `labels_written` (NOT NULL, default false), that says whether the labels are the document's own answer (catalog-046). A row with `labels_written` false is legacy: its labels should be NULL and a reader falls back to the shared chunk row's label. That is the invariant this step adds, **`labels_written OR (section_title IS NULL AND section_type IS NULL AND page_number IS NULL)`**: a row that holds a label is a written row. Nothing enforces it today; the census counts the rows where it does not hold, and the writer audit below says why none is expected. This runbook adds it as a table CHECK, `catalog_document_chunks_labels_written_check`, so a later writer cannot break it. It is an operator step with three engine routes. Nothing calls them; no startup, upgrade rung, hook, doctor row or schedule does.

It is the same step as the [manifest chunk-index CHECK](manifest-chunk-index-check.md) (one class, `ManifestCheckAdmin`, parameterised by `ManifestCheck`) and shares its guards, refusals, answers and count machinery. Read that runbook, and the [catalog TTL CHECK](catalog-ttl-check.md) behind it, for what this page only names. What differs is the predicate, the constraint, the run lock and the route family.

## The routes

All `POST`, root credential only, under `/v1/admin/manifest-labels-check`, bodies ignored: `/census`, `/add`, `/validate`. Their answers, in the same order as the chunk-index routes':

| Route | What it does |
|---|---|
| `/census` | Read only. Per tenant: `rows` and `violating_rows` (`labels_written` false and a label not NULL); the total, the tenants checked, the table rows examined, and the constraint's state. |
| `/add` | A constraint that exists answers `present` at once, with no count. Otherwise counts every tenant and answers 409 `tenant_violates_constraint` naming each tenant with a violator, else adds the CHECK NOT VALID, then counts again and reports `violating_after_add`. |
| `/validate` | `already_valid` and `absent` answer at once, with no count. Otherwise counts every tenant and answers 409 `tenant_violates_constraint` naming each tenant with a violator, else runs `VALIDATE CONSTRAINT`. |

Refusals: 503 `admin_cannot_see_all_tenants` (an admin role that cannot read every row), 503 `admin_probe_undercount` (a census below a tenant-scoped count), 409 `constraint_run_in_progress` (another run of this step is executing its constraint statement), 503 `migration_in_progress`, 503 `lock_timeout` / `read_timeout`. The count is every tenant's; a tenant with no `service_tokens` row is counted.

**Order:** census (expected to read zero), add, read `violating_after_add` in its answer, census again, validate. Validate changes no data (SHARE UPDATE EXCLUSIVE, which lets writers through).

**Run locks and table locks.** The step has its own advisory run-lock key, distinct from the chunk-index step's, the TTL step's and the chunk-table steps', so a run of one never answers another's 409 `constraint_run_in_progress`. That is true of the run lock only. Two censuses as the table owner (this step's and the chunk-index step's, or two of this step's) both take the ACCESS EXCLUSIVE table lock, so the second waits behind the first and answers 503 `lock_timeout` after 5 seconds.

**Synchronous.** The census (up to 10 minutes, 60 seconds as the table owner) and validate (up to 30 minutes) answer when they finish. The public edge gives up on a request after about 30 seconds; the engine carries on. Add and validate count first and take the run lock only for the constraint statement, so 409 `constraint_run_in_progress` answers a retry only once that statement is running. A retry during the count runs a second count: as a superuser or BYPASSRLS role it runs alongside the first; as the table owner it waits behind the first count's table lock and answers 503 `lock_timeout` after 5 seconds. Do not retry on the edge timeout: wait, then call the census to see where it stands (an add that finished answers `present`, a validate that finished `already_valid`).

**The count is every tenant's, and the owner's count queues manifest access.** `nexus.catalog_document_chunks` is FORCE row-level-security with no policy that lets its owner read every row. As the table owner (the managed service's `nexus_admin`) the census lifts FORCE for one transaction that is always rolled back, under an ACCESS EXCLUSIVE lock on the manifest for the scan, so every manifest read and write waits behind it. While the ALTER waits for that lock it queues too: every new manifest access lines up behind it, and the engine request threads blocked there hold their pool connections. The manifest is the largest catalog table: run the census, and an add or validate that has to count, in a quiet window. The full account is in the [chunk-index runbook](manifest-chunk-index-check.md#the-count-is-every-tenants).

**If the census names a violator.** The add route refuses until none is left, because a NOT VALID CHECK is evaluated on every row version written, and a row that breaks it could no longer be updated. Fix the rows first; no route does it. A violator holds a label that no marker vouches for, so the choice is the operator's: set `labels_written` true (the labels are then that document's own answer; readers stop falling back to the chunk row's label for it), or set the labels to NULL (the row is legacy again and a reader falls back as before). A row whose labels came from the chunk row's last writer (the contamination RDR-108 exists to end) is the second case.

## Writer audit (2026-11-14, nexus-6pfgb.42)

The census is expected to read zero because no writer in this repository writes a label with `labels_written` false. The audit, by writer:

| Writer | Writes a label with the marker false? |
|---|---|
| `CatalogRepository.insertManifestChunkRows`, the one manifest INSERT (manifest write, write_many, append, append_many, import, the re-embed move), all three arms (plain, `UPSERT_APPEND`, `UPSERT_IMPORT`) | Not from the engine's own logic: the labels and the marker are columns of one row, the marker is true whenever the request row carries any label key, and every ON CONFLICT arm sets all four from the same `excluded` row. **One caveat:** an explicit `labels_written` boolean in the request wins, so a request that says `labels_written: false` beside a non-NULL label is stored as sent. No client in this repository sends it: the Python wire (`manifest_row_wire`, `_manifest_chunk_rows`, `note_manifest_rows`) sends back the stored pair (NULL labels and false, or labels and true) or sends label keys with no explicit false. After the CHECK such a request is refused, with SQLSTATE 23514 on that row; that is the CHECK's purpose. |
| `ManifestFill` (label rung) | No: sets the three labels and `labels_written = true` in one UPDATE, guarded by `labels_written` false on the target row. Its span rung touches neither. |
| `ManifestLabelStage` | Reads only. |
| Quarantine-restore reattach (plpgsql, vectors-025, 030, 053), the manifest backfill | No: they name neither the labels nor the marker, so a new row takes NULL labels and false. |
| Tenant import (`TenantImportLoad`, one INSERT ... SELECT of the export file's columns) | Pass-through: writes the pair the source row held, so it writes a violator only from a source row that already was one. Count the source engine first. |
| Collection rename (`ChashRepository.renameCollection`) | No: it UPDATEs `collection` and `embedding_model` only. No live code UPDATEs a manifest `chash`. A collection re-home that moves documents (the re-embed move) goes through `insertManifestChunkRows`, the first row. |
| Triggers, changesets | No: the stamp triggers are statement-level and write `chunk_orphaned_at`; the `chunk_index` default trigger touches `chunk_index` only; catalog-046 adds the columns; no changeset sets a label or the marker. |

The chunk-key sweep's dry run already counts the same rows per tenant as `manifest_unwritten_label_rows`, informationally; this step's census is the all-tenant, admin-connection version of that count and is the one an add waits on.

## Why it is a step of its own

Same reasons as the chunk-index step: not a Liquibase changeset (a boot-time VALIDATE crash-loops an install whose rows differ from the measured one, and a release does not rewrite user data at engine boot); the count is read on the admin session with the table's FORCE row-level security lifted for the owner; add needs ownership only; validate needs ownership and no row visibility. The hazards of the count and of a retry are in the paragraphs above.

## Not wired, and not yet run

No client verb sends these routes. The census changes no data, but as the table owner it holds an ACCESS EXCLUSIVE lock on the manifest while it scans.
