# Migration Recovery (RDR-112, nexus-uvv1)

The T2 daemon is the sole migration runner for `memory.db` and
`tuples.db` (RDR-112 §9). Migrations run before the daemon binds
sockets, so no client ever sees a partially-migrated schema. A
migration that fails partway through the registry is still survivable:
the daemon takes a snapshot before it touches the database.

## What gets backed up

Before `run_daemon_migrations` calls `apply_pending`, the daemon copies
`memory.db` to a sibling backup file:

```
~/.config/nexus/memory.db.bak-<from_version>-<unix_ms>
```

- `<from_version>` is the version stored in `_nexus_version.cli_version`
  before any migration runs (for fresh installs it is `0.0.0`).
- `<unix_ms>` is the wall-clock time of the snapshot in milliseconds.
- The copy uses SQLite's `Connection.backup()` (online backup API).
  It cooperates with WAL mode and produces a consistent snapshot of
  every committed page.

Backups are taken only when migrations would actually run, i.e. when
`from_version != current_version`. Same-version no-op opens skip the
snapshot.

The latest three backups are kept; older ones are pruned automatically
to cap disk use. Adjust the retention with care: each backup is
roughly the size of the live database.

## Opting out

```
export NX_MIGRATION_BACKUP=0   # also "false" / "False"
```

Tests under `tmp_path` use this to keep fixtures fast. Production
should leave it unset.

## Recovering from a failed migration

If `run_daemon_migrations` raises and the daemon refuses to start, the
log line that fired right before the failure pins which migration
broke:

```
INFO migration_step_start  name="..." introduced="x.y.z"
ERROR migration_step_failed ...        # or unhandled exception
```

Recovery procedure:

1. **Stop the daemon** (if any process is still up):

   ```
   nx daemon t2 stop
   ```

2. **Inspect the backups** beside the live database:

   ```
   ls -lt ~/.config/nexus/memory.db.bak-*
   ```

   The newest one was taken right before the migration ran. Its
   `from_version` tells you what schema state it captures.

3. **Decide on a remedy**:

   - **Roll back and downgrade `conexus`** so the registry's
     `current_version` falls back below the failing migration. The
     daemon now sees `from_version == current_version` and skips the
     bad migration on next start:

     ```
     cp memory.db memory.db.failed-$(date +%s)
     cp memory.db.bak-<from_version>-<ms> memory.db
     uv tool install --reinstall conexus==<previous_version>
     nx daemon t2 start --foreground
     ```

   - **Stay on the current `conexus` and fix the migration in place**
     by editing the failing function in
     `src/nexus/db/migrations.py`, then re-running. Keep the original
     backup until you've verified the fix.

   - **Force re-run from the backup**: `nx upgrade --force` resets the
     stored version to `0.0.0` and reapplies the whole migration list
     against the backup-restored database. Use only when individual
     migrations are confirmed idempotent.

4. **Verify integrity** before re-binding sockets:

   ```
   sqlite3 memory.db "PRAGMA integrity_check;"
   sqlite3 memory.db "SELECT value FROM _nexus_version WHERE key='cli_version';"
   ```

5. **Restart the daemon** and confirm the migration log records
   `migration_step_done` for every migration up to `to_version`.

## Why an outer `BEGIN/COMMIT` is not used

A wholesale transaction wrapper around the migration list looked
tempting but several entries call `conn.commit()` mid-function (large
backfills, `executescript` calls, DDL touching FTS5 virtual tables).
Wrapping those in an outer transaction would either re-enter SQLite or
fight WAL-mode invariants for `ALTER TABLE`. The hybrid approach is:

- Each migration is responsible for its own atomicity boundaries.
- A pre-migration backup gives the operator a clean rollback point if
  any migration partial-fails.
- `MigrationRetry` (the sanctioned partial-failure path, e.g. catalog
  not yet present) is unchanged: it returns and asks for a retry on
  next open without marking the run done.

See `apply_pending` and `run_daemon_migrations` in
`src/nexus/db/migrations.py` for the implementation.
