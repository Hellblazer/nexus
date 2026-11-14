# Backup and restore

This page has two parts. [Local mode](#taking-a-backup) backs up the local engine's whole database with `pg_dump` and restores it by hand into a fresh database. [Cloud mode](#cloud-tenant-backups-managed-mode) backs up your own tenant of the managed service into one `.nxtenant` file and checks it offline.

`nx backup` in local mode writes one file holding the local engine's whole database. It is a verb you run. Nothing runs it for you: `nx upgrade`, `nx doctor`, `nx init`, `nx self`, the hooks and the daemons never call it, and a test fails on a Python import, a subprocess command line or a hook or script line under the repository's code directories that would (it cannot see a command assembled at run time). Bead nexus-f3yqd.4.

There is no automatic backup; this file is the only copy besides the live database. Copy it to another disk or machine.

The rest of this part is local mode. In managed (cloud) mode `nx backup` takes the cloud arm, described [below](#cloud-tenant-backups-managed-mode).

## Taking a backup

```
nx backup                      # ~/nexus-backups/nexus-<UTC yyyymmddThhmmssZ>.dump
nx backup --to /Volumes/Big/nexus-before-upgrade.dump
```

- The engine's Postgres must be running (`nx daemon service start`). The engine itself keeps serving while the dump runs.
- The dump is `pg_dump -Fc` from the Postgres bundle the engine runs, as one repeatable-read snapshot: consistent, every tenant included. The bundle is built without zlib, so the dump is **not compressed**: expect a file about the size of the database.
- It prints the database's size on disk first (tables, indexes and bloat; indexes are not dumped, so the file can be smaller or larger), then the bytes written and the rate while it runs. If the destination disk has less free space than the database occupies it warns and carries on.
- `pg_dump` waits at most 60 s for a table lock. When it gives up (its own error reads `canceling statement due to statement timeout` on a `LOCK TABLE`), `nx backup` fails with a message saying something holds locks on the database, most likely a schema migration the engine is running (`nx doctor` says so); run `nx backup` again once it is done.
- The finished file is checked with the bundled `pg_restore -l` before it is kept. A file that does not verify is removed and the verb fails.
- An existing destination is refused and never overwritten. The dump goes to a `.partial` file next to the destination and is moved into place only when it succeeded and verified; a failure, Ctrl-C, a kill (SIGTERM) or a closed terminal (SIGHUP) removes it. An unwritable or too-long destination is a one-line refusal.
- The default directory is outside the engine's data directory on purpose: a backup inside `~/.config/nexus` goes with the install when it is removed. The file is created readable by you only (on Windows through the same owner-only ACL helper the credentials files use, applied before the first byte is written); a directory it creates is owner-only on macOS and Linux, and on Windows has whatever the parent folder's ACL gives, so choose the folder with that in mind. The file holds all your data; treat it that way.
- At the end it prints the path, the size, the exact restore commands with this machine's bundle path filled in, and where the full runbook is.

## Restoring

There is no `nx restore`: a restore over a live database is destructive, so it is a runbook you follow by hand. The safe way restores into a **fresh database next to the live one** and only then swaps names. Nothing is overwritten until the swap, and the swap moves the live database aside instead of deleting it.

What each step touches:

| Step | Touches |
|---|---|
| 0. Back up the live database | Writes a new file. |
| 1. Stop the engine, make sure Postgres is up | Nothing on disk. |
| 2. Create `nexus_restored` and `pg_restore` into it | Only the new database `nexus_restored`. Fails if one by that name exists. |
| 3. Check for connections, swap names | Renames `nexus` to `nexus_before_restore` and `nexus_restored` to `nexus`, in one transaction. No data is deleted. |
| 4. Start the engine and check | Nothing. |
| 5. Undo (only if you want the old database back) | Renames them back, in one transaction. |
| 6. Drop the database you replaced | **Deletes it for good.** Only once you are satisfied. |

### Before you start

You need five values. `nx backup` prints the first four in its restore commands; they are also on disk:

- `<pg_bin>`: the bundled Postgres `bin` directory, the same one the engine runs: `<config dir>/pg-bundle/bundle/bin`, or the directory `NEXUS_PG_BIN` names. Never a Postgres you installed yourself: the dump's version and extensions (pgvector) belong to the bundle.
- `<port>`: `PG_PORT` in `<config dir>/pg_credentials`.
- `<user>`: the cluster's superuser. On macOS and Linux that is your login name; on Windows it is a role named `nx_` plus 16 hex digits, so take it from the commands `nx backup` printed.
- `<dump>`: the file `nx backup` wrote.
- `<pgdata>`: `PG_DATA` in `pg_credentials`; only the "start Postgres" command in step 1 needs it.

The config dir is `~/.config/nexus` (`%USERPROFILE%\.config\nexus` on Windows), or `NEXUS_CONFIG_DIR` when set. The superuser's password is `PG_SUPERUSER_PASS` in `pg_credentials`; put it in the environment, never on the command line:

```
export PGPASSWORD='<PG_SUPERUSER_PASS>'        # macOS / Linux
$env:PGPASSWORD = '<PG_SUPERUSER_PASS>'        # Windows PowerShell
```

To restore on a machine with no nexus database (a new box, a reinstall), run `nx init` first: it creates the cluster and the roles the dump's grants refer to. Then follow the same steps; the `nexus` database `nx init` made is the one that is moved aside.

### 0. If the live database still reads, back it up first

Run `nx backup` now, to a different file. Step 6 deletes the database you replace, and anything written after the backup you are restoring exists only in that database.

### 1. Stop the engine and make sure Postgres is up

```
nx daemon service stop
```

This stops the supervisor and the engine. In the code, `stop` leaves Postgres running (it says so: "by design"), and Postgres is needed for the steps below. On Linux an autostart unit managed by systemd may end processes the supervisor started when the supervisor exits; that has not been verified here, so check that Postgres is answering:

macOS and Linux:

<!-- nx-restore-commands: posix pg_up -->
```
"<pg_bin>/psql" -h 127.0.0.1 -p <port> -U <user> -d postgres -t -A -c "SELECT 1"
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 pg_up -->
```
& "<pg_bin>\psql.exe" -h 127.0.0.1 -p <port> -U <user> -d postgres -t -A -c "SELECT 1"
```

It prints `1`. If it cannot connect, start Postgres alone, with the same arguments the engine's supervisor uses (on Windows, run it from a console you keep open until step 4 is finished):

macOS and Linux:

<!-- nx-restore-commands: posix pg_start -->
```
"<pg_bin>/pg_ctl" -D "<pgdata>" -l "<pgdata>/pg.log" -o "-p <port>" start -w
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 pg_start -->
```
& "<pg_bin>\pg_ctl.exe" -D "<pgdata>" -l "<pgdata>\pg.log" -o "-p <port>" start -w
```

### 2. Restore into a fresh database

macOS and Linux:

<!-- nx-restore-commands: posix restore -->
```
"<pg_bin>/createdb" -h 127.0.0.1 -p <port> -U <user> nexus_restored
"<pg_bin>/pg_restore" -h 127.0.0.1 -p <port> -U <user> -d nexus_restored --exit-on-error "<dump>"
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 restore -->
```
& "<pg_bin>\createdb.exe" -h 127.0.0.1 -p <port> -U <user> nexus_restored
& "<pg_bin>\pg_restore.exe" -h 127.0.0.1 -p <port> -U <user> -d nexus_restored --exit-on-error "<dump>"
```

`--exit-on-error` stops at the first problem instead of leaving a half-restored database. If it stops, or you interrupt it, drop `nexus_restored` (the `drop_restored` command in step 6) and read the message before trying again; the live `nexus` database has not been touched.

**This step rebuilds every index, the HNSW vector indexes included, and prints no progress.** On a large database it takes as long as the first index build did (minutes to hours). Do not interrupt it.

### 3. Check for connections, then swap the databases

The rename fails ("database is being accessed by other users") while anything is connected to `nexus`. This prints the number of connections to it; it must be `0`:

macOS and Linux:

<!-- nx-restore-commands: posix check -->
```
"<pg_bin>/psql" -h 127.0.0.1 -p <port> -U <user> -d postgres -t -A -c "SELECT count(*) FROM pg_stat_activity WHERE datname = 'nexus'"
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 check -->
```
& "<pg_bin>\psql.exe" -h 127.0.0.1 -p <port> -U <user> -d postgres -t -A -c "SELECT count(*) FROM pg_stat_activity WHERE datname = 'nexus'"
```

The swap is one `psql` command holding both renames, which Postgres runs as one transaction: either both happen or neither does.

macOS and Linux:

<!-- nx-restore-commands: posix swap -->
```
"<pg_bin>/psql" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "ALTER DATABASE nexus RENAME TO nexus_before_restore; ALTER DATABASE nexus_restored RENAME TO nexus"
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 swap -->
```
& "<pg_bin>\psql.exe" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "ALTER DATABASE nexus RENAME TO nexus_before_restore; ALTER DATABASE nexus_restored RENAME TO nexus"
```

### 4. Start the engine and check

```
nx daemon service start
nx doctor
```

**The first boot after restoring a backup from an older version runs that version's schema migrations**, as any database of that age would. It can take a long time on a large store and prints "The engine is migrating its database schema" while it does. Do not stop it; stopping restarts the step.

### 5. Undo the swap

To get the database you replaced back, stop the engine again (`nx daemon service stop`), check that `nexus` has no connections (the step 3 check), then run this. It moves the restored database back to `nexus_restored` and the old one back to `nexus`, in one transaction. It only works while the names are as the swap left them.

macOS and Linux:

<!-- nx-restore-commands: posix undo -->
```
"<pg_bin>/psql" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "ALTER DATABASE nexus RENAME TO nexus_restored; ALTER DATABASE nexus_before_restore RENAME TO nexus"
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 undo -->
```
& "<pg_bin>\psql.exe" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "ALTER DATABASE nexus RENAME TO nexus_restored; ALTER DATABASE nexus_before_restore RENAME TO nexus"
```

### 6. Drop what you no longer need

Only when the data in `nexus` is what you wanted. This deletes `nexus_before_restore` for good:

macOS and Linux:

<!-- nx-restore-commands: posix drop -->
```
"<pg_bin>/psql" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "DROP DATABASE nexus_before_restore"
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 drop -->
```
& "<pg_bin>\psql.exe" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "DROP DATABASE nexus_before_restore"
```

And to throw away a restore you abandoned (a failed or interrupted step 2, or after an undo), delete `nexus_restored`:

macOS and Linux:

<!-- nx-restore-commands: posix drop_restored -->
```
"<pg_bin>/psql" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "DROP DATABASE nexus_restored"
```

Windows (PowerShell):

<!-- nx-restore-commands: win32 drop_restored -->
```
& "<pg_bin>\psql.exe" -h 127.0.0.1 -p <port> -U <user> -d postgres -c "DROP DATABASE nexus_restored"
```

## Why these commands are trustworthy

The commands above are not hand-typed twice. `nexus.backup.pg_backup.restore_sections` renders them, `nx backup` prints the restore commands with the real bundle path, and `tests/test_nx_backup.py` reads the fenced blocks on this page, puts in scratch database names, fills in the placeholders and runs them (the restore, the connection check, the swap, the undo, both drops) against a real dump on a real test cluster, with nothing on PATH. A drift between this page and the code turns that test red. The start-Postgres command is compared with the argv the engine's own start code builds. The Windows blocks are compared with the renderer's output on every platform, but the test executes only the macOS and Linux blocks; the Windows ones have not been run against a real dump.

## Cloud tenant backups (managed mode)

RDR-248, beads nexus-csayy.2 (the engine's routes) and nexus-csayy.3 (the client). A cloud user holds a token, not the database, so the engine writes the backup: it reads ONE consistent snapshot of your tenant and serves it in pages, and `nx backup` joins the pages into one file. It is still a verb you run; nothing runs it for you, and the only-copy sentence above applies to this file too.

```
nx backup                                   # ~/nexus-backups/nexus-<tenant>-<UTC yyyymmddThhmmssZ>.nxtenant
nx backup --to /Volumes/Big/before-move.nxtenant
nx backup --check ~/nexus-backups/nexus-acme-20261112T190000Z.nxtenant
```

### What you need

- A managed install (`nx config` shows a `service_url`) and a **tenant or data token** for your tenant. The engine's operator (root) token is refused by the export routes with `export_scope_forbidden`; the verb says so and names what to use instead (`nx config set service_token <token>`). A data token from `mint_token` is re-minted on its own.
- An engine that has the export routes: engine 0.1.191 or later. The verb reads `tenant_export_supported` from the engine's `/version` first. An engine without it is refused before any session opens, naming 0.1.191. A current engine behind a public edge that does not yet relay that field reads the same way; the refusal says so, and the fix is the edge's allowlist, not the client. The export format is read from the session's own header (`format_version`): an unknown one is refused and the session closed. Who upgrades the managed engine is the operator's decision, not the client's.

### What the file holds

Every `data` and `derived` table of your tenant from one snapshot: the catalog (documents, links, chunk manifests, collections), T3 chunks with their embeddings, T2 notes, plans, document aspects and highlights, taxonomy, the rebuildable telemetry and frecency tables, and the `embedding_models` rows the chunks refer to. A sensitive document (RDR-243) is in the file like everything else, and the verb says how many the file holds; the file is created readable by you only (mode 0600; the directory it creates 0700), so keep it that way.

It leaves out operational state: tuples and their claims and deliveries (their ids hash the tenant, so they cannot move), in-flight PDF and re-embed jobs, session scratch (T1), and the request ledger, tier writes, routing events and the other logs. The verb prints this after every run; a CI test checks that the table names behind the list it prints are the engine's own excluded tables (it cannot check the wording of the phrases, so a reclassified table makes CI fail until someone updates the list). No credential is in the file: tokens live in global tables the export cannot read.

### How a run goes

1. `GET /version`: the export flag only (the public edge relays capability fields as booleans; the export format comes from the session header in the next step, and an unknown one is refused). A token with a control character in it is refused before this, without printing it. Then `POST /v1/tenant/export` opens your session (one per tenant). Run one `nx backup` per tenant at a time: a second run replaces the first's session, the two keep replacing each other, and both end saying the engine forgot the export session. The engine holds a read-only snapshot for it and reads no table, so other tenants and tenant creation are not held up.
2. Every table, from its first page, page by page (each page about 16 MB, cut by bytes), each checked on arrival: its SHA-256 and its row count against the engine's headers. A failed page is asked for again from the same key, up to five tries with waits of 1, 2, 4 and 8 seconds. An engine that answers busy (a tenant create holding a lock, or a page still in flight) is waited on for the `Retry-After` it gives, up to about two minutes per request (one last wait can overshoot by one `Retry-After`), without using the tries; a full engine (no free export slot) is waited on for up to five minutes, saying so. When the tries or the wait run out the session is closed, the partial file removed and the verb says to run it again.
3. A token that expires mid-run is replaced (re-minted, or re-read from config) and the SAME page is asked for again: the session belongs to your tenant, not to the token.
4. If the engine forgets the session (it restarted, or the request reached another instance), the verb starts over with a new one, at most twice. A session is also ended by the engine 10 minutes after its last page and 60 minutes after it opened. If that happens, the verb names the 60-minute limit; a tenant whose export needs longer than that fails loudly instead of holding the database's snapshot open (RDR-248 A5).
5. The session is closed (DELETE), the file is checked again from disk, and only then moved into its name. An existing destination is refused, never overwritten, at the start and again at the move.

Ctrl-C, SIGTERM and SIGHUP stop the run, remove the partial file (`.<name>.partial-<pid>` beside the destination) first, and then send a best-effort DELETE so the engine drops the snapshot at once instead of after its idle bound; a second signal during the DELETE cannot leave the file behind. A killed process (SIGKILL) cannot clean up: remove a leftover partial by hand; the engine closes the abandoned session itself, and the next run deletes it first if it is still listed.

How long it takes is the connection's rate: the database reads a large tenant in about a minute and a half; the rest is the transfer (about 6 minutes for 3.8 GB at 10 MB/s). The first real cloud run records the actual rate (RDR-248 Activation Step 2).

### Checking a file

`nx backup --check FILE` reads the file and contacts nothing (no engine, no token, either mode). It checks the frame structure, each page's SHA-256 and row count, each table's count and SHA-256 against the file's trailer, and one SHA-256 over the whole file, then prints the header: tenant, engine version, export format, schema changeset count, snapshot time, and every table with its class, rows and pages. A file that does not verify is refused naming the table at fault, and nothing is printed as OK. A truncated copy (an interrupted download of the file, a full disk) reads "the file is truncated in ..."; a flipped byte in a page, "table X page N: its hash is ...".

The `.nxtenant` layout is described in `src/nexus/backup/tenant_file.py` (a magic, then header, table, page and trailer frames; each page is the engine's own binary COPY stream, kept byte for byte).

### Restoring

`nx backup restore FILE` (RDR-248, nexus-csayy.6) loads a `.nxtenant` file into your own, EMPTY tenant through an engine import session; it can never overwrite data. The local `pg_restore` runbook above is for the local engine's whole database and does not apply to a `.nxtenant` file.

The order, so nothing is lost:

1. The operator runs the current engine (0.1.192 or later, the first with the import routes), creates the empty tenant and issues its token. Nobody pins an engine version: a file from an older engine loads when its columns match, and the verb prints the schema changeset difference. A restore needs a tenant or data token; the operator's root token is refused.
2. You set that token (`nx config set service_token <token>`) and run `nx backup restore FILE`. On a **local engine** the install's own token is the supervisor's root token and is refused, so issue a tenant token first: `nx service token issue --tenant <tenant>` (`default` for the install's own tenant, which must be empty), then `export NX_SERVICE_TOKEN=<token>` before the restore (a local install reads its token from that variable). Do not use the tenant until the verb reports the outcome: a write during the upload makes the engine refuse the load at its last check, and nothing is written.
3. The verb verifies the whole file first, uploads it in parts of at most 8 MiB, commits, and follows the load. The load is one transaction, so it lands whole or not at all. When it ends the verb prints the rows loaded by class, the tables and columns it skipped, and how many soft-reference values were negated or dropped (rows that named a memory, plan or topic that was already gone when the backup was taken keep a negative id, so a second backup and restore keeps them negative).

If your connection drops during the load the engine carries on; the verb reads the status again, and after an engine restart it reads the tenant's restore marker and says the load committed only when the marker names your file. If it prints that the outcome is unknown, the load may still be running or may have committed: it does not cancel it. Do not restore again at once; run `nx backup restore FILE` once more later. A tenant that holds the file's marker answers "this file was already restored", and an import that is still loading is left alone. A tenant that is not empty is refused, naming the table, before anything is uploaded. Another restore loading on the same database makes the commit wait: the verb tries again every 60 seconds, and gives up when the engine closes the session at its bound.

#### The operator's order (RDR-248, nexus-csayy.7)

The restore only works in this order, because the verb refuses a tenant that holds anything but the rows the engine writes itself. The verb never creates a tenant and never issues a token.

1. **Run the current engine.** A file from an older engine loads when its columns still exist and have the same types in the engine that receives it, so nobody pins the engine to the file's version. The engine reports `tenant_import_supported` on `/version`; without it the verb refuses (see the table below).
2. **Create the empty tenant and issue its token.** Managed service: the operator (`POST /v1/tenants/create`, the tenant's first token is in the answer). Local engine: `nx service token issue --tenant <tenant>`; `default` is the install's own tenant and must be empty. Create the tenant with its own name: the load lands in the tenant of the token, whatever tenant the file came from. Give the token only to the person who restores; a restore refuses the operator's root token (`import_scope_forbidden`).
3. **The user restores.** Managed: `nx config set service_token <token>`. Local: `export NX_SERVICE_TOKEN=<token>`. Then `nx backup restore FILE`. Nothing else uses the tenant until the verb reports the outcome: a write made during the upload fails the load at its last check (`failed: tenant not empty: <table>`), nothing is written, and the tenant is then no longer empty, so it needs a fresh one.
4. **Leave the engine running.** One load runs at a time on a database. A second restore (any tenant) is told `restore_busy` and keeps its upload staged; creating a new tenant while a load runs is told `tenant_creation_busy` with `Retry-After: 30` and succeeds once the load ends. A token for a tenant that already exists is not delayed.

#### An engine restart, or a lost connection, during a restore

The load is one transaction on one connection the engine holds. The engine keeps the outcome in memory for 24 hours, and the tenant's restore marker (`catalog_meta` key `rdr248_restore`) is written as the transaction's last statement, so it is the one record that survives a restart.

- **The client loses its connection.** The load carries on. The verb reads the status route again until it reads `committed` or `failed`; if it cannot, it says the outcome is unknown, exits 1 and leaves the load alone. Run `nx backup restore FILE` again later.
- **The engine restarts before or during the load.** The transaction ends with the connection and rolls back, and the session is gone. The verb reads `no such session`, then the marker. A marker that does not name this file means nothing was loaded, the tenant is as it was, and the verb says to run the restore again.
- **The engine restarts after the commit.** The marker names the file (`header_sha256` is the SHA-256 of the file's header frame), and the verb reports the restore committed, printing the marker's session, time and row counts in place of the engine's report.
- **Running the verb on the same file again is always safe.** A tenant that holds this file's marker answers "this file was already restored into this tenant"; a tenant with a load still running is left alone and the verb says to wait; a tenant that is empty gets a fresh restore. Do not delete the session of a load that may be running: `DELETE /v1/tenant/import/<id>` cancels it.
- **Reading the marker by hand:** `GET /v1/tenant/import-marker` with the tenant token answers `{"present": true, "marker": {"header_sha256", "session_id", "committed_at", "row_counts"}}` when a load committed, and `{"present": false}` otherwise. `nx backup --check` does not print `header_sha256`; to learn whether a given file committed, run the verb on it.

#### What each refusal means

The verb prints the engine's own words; this is what they mean and what to do. Unless the line says otherwise, nothing was written.

| The verb or engine says | Meaning | What to do |
| --- | --- | --- |
| `... does not verify` | The file is damaged or truncated (the same check as `nx backup --check`). Nothing was sent. | Take the backup again, or use another copy. |
| no `tenant_import_supported`, "has no tenant import routes" | The engine is older than the import routes (0.1.192), or the public edge does not relay the field. | The operator upgrades the engine or the edge. |
| `import_scope_forbidden`, "rejected the token (401)" | The token is the operator's root token, or is not current. | Use the tenant's own token (step 2 and 3 above). |
| `tenant_not_empty`, naming a table | The tenant holds rows other than the ones the engine writes itself. When the marker names this file, the verb says "already restored" instead. | Restore into a new, empty tenant. |
| `session_exists` | The tenant has an import or export session on the engine. The verb deletes one that is staging or finished and leaves one that is loading. | Wait: the engine closes an idle session after 10 minutes. |
| `import_capacity_full` | The engine's session slots (shared by export and import) are all used. The verb waits and retries for a few minutes. | Try again later. |
| `singleton_lock_not_held` | The engine cannot show it is the only engine on the database, so it will not hold sessions in memory. | The operator's: one engine per database, and the lock not switched off. |
| `unsupported_format_version` | The file's export format is not one this engine reads. | Restore with an engine and `nx` that read it. |
| `file_not_loadable`, naming a table or column | The file has a column this engine lacks (and has not retired), a column of another type, a table it cannot place, or lacks a NOT NULL column that has no default; or a child table came without its parent. | The file is older or newer than this engine's schema by more than the compatibility rule covers. Tell whoever runs the engine; the message names what differs. |
| `missing_embedding_model` | The file's chunks use an embedding model the engine has no row for. | The operator adds the model, or restores into an engine that has it. |
| `import_too_large` | The file's declared bytes, or the staged bytes so far, pass `NX_IMPORT_MAX_BYTES` (8 GiB by default). | The operator raises the cap, if the database has room. |
| `restore_busy` (a commit) | Another restore holds the database's one load slot. The upload stays staged. | Nothing: the verb retries every 60 seconds until the engine's load bound (`NX_IMPORT_LOAD_MAX`, 60 minutes by default) passes, then the session closes; run the verb again. |
| `failed: part T/k: ...`, `part_failed` | A part could not be staged (a full disk, a malformed part). The session is closed. | Run the verb again; if it repeats, tell the operator (disk space on the database). |
| `failed: tenant not empty: <table>` | Something wrote to the tenant between the open and the load. | Use a fresh tenant; keep it unused until the verb reports. |
| `failed: verification: ...` | The load's own count or reference check failed, for instance a row that names a parent the file does not hold. The transaction rolled back. | Keep the message and the file and tell whoever runs the engine; do not retry blindly. |
| `failed: ... bound` (`idle`, `total`, `restore-wait`, `the load passed NX_IMPORT_LOAD_MAX`) | A time bound closed the session and the transaction rolled back. | Run the verb again on a faster link, or ask the operator for a larger `NX_IMPORT_LOAD_MAX`. |
| `failed: aborted`, `failed: evicted`, `failed: engine stop` | The session was deleted, the tenant was reset, or the engine shut down. Rolled back. | Run the verb again. |
| "the outcome is unknown" (exit 1) | The verb lost the engine during the load and could not read the status or the marker. | Do not restore again at once. Run the verb on the same file later: it says whether the load committed. |
