# Backup and restore of the local engine's database

`nx backup` writes one file holding the local engine's whole database. It is a verb you run. Nothing runs it for you: `nx upgrade`, `nx doctor`, `nx init`, `nx self`, the hooks and the daemons never call it, and a test fails on a Python import, a subprocess command line or a hook or script line under the repository's code directories that would (it cannot see a command assembled at run time). Bead nexus-f3yqd.4.

There is no automatic backup; this file is the only copy besides the live database. Copy it to another disk or machine.

Local mode only. In managed (cloud) mode `nx backup` refuses: cloud backups are on the conexus side.

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
