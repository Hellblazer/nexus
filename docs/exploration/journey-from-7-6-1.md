# From 7.6.1 to 8.1.2

It’s been a long journey from Milan to Minsk - wait.  Wrong show.   In the Nexus show, there’s been a lot of work in the last three months.   The storage became Postgres only: no SQLite and no ChromaDB.  The vector search got one partition per embedding model and a check on the search’s own recall.  Search answers now tell you when the evidence is thin. Documents can now be marked sensitive to keep them out of the LLM.  The plugin now installs the command line nx tooling for you, simplifying the installation.  Nexus is now officially supported on Windows and the local engine and Postgres service are native executables.  Agents can now wait for each other, ask a person for a decision, and run small programs in a sandbox. The first upgrades to 8.0.0 were slow and silent on large stores, and 8.1.0 to 8.1.2 fixed that.

This post goes through the changes one theme at a time. For each change, it says what changed, what it means if you use Nexus, and the Nexus release where you first see it. Near the end, a short list says what an upgrade from 7.6.x does by itself and what it leaves for you to do.

## Storage: Only Postgres and nothing else

Since 7.23.0, Nexus does not use SQLite databases at all. Everything is now in Postgres, behind the engine: session scratch, memory, the catalog and the vectors. So you have one store to back up, and one place to ask about your data. If `nx doctor` finds an old `memory.db` or `.catalog.db` file, it tells you that you can safely delete it.

Since 7.74.0, the local cluster also has a password. Before, it trusted every local connection. New clusters use scram-sha-256 with a superuser password. An existing cluster changes to this on its next service start. A local tool that connected to the local Postgres cluster without a password now needs the password that the service keeps.

From 7.95.0, the engine does not start on a PostgreSQL older than 17. If you try, it logs `pg_version_below_floor` and stops before it accesses the schema. If your local data directory came from an older PostgreSQL, use `nx daemon service pg-upgrade --old-bin <bin dir>`, from 7.99.0. It is a dry run until you add `--apply`.

On the managed service, the automatic upgrade at session start does less work since 7.91.0. It does not read the whole catalog, and only one upgrade runs at a time for each config directory. The data steps that it skips run when you run `nx upgrade` yourself, one time, from one machine.

## Vector search: one table per model, and a check on recall

Since 7.73.0 (engine 0.1.150), every embedding model has its own partition of the chunk table. The engine divides the chunk table by model and then by tenant. So a search reads only your data, embedded by one model. Two embedding models with the same dimension no longer share an HNSW index. On a local install, this migration is one-way. The first engine start rewrites every vector into the new partitions. It needs free disk space of at least 2.2 times the size of the vector tables.

In 7.77.0, three days later, large collections got their own index. A collection with 20,000 rows or more gets its own partial HNSW index. The engine builds these indexes one at a time, without blocking writes. The engine searches smaller collections exactly. A filtered search of the shared index can miss the nearest rows of a small collection, and an exact scan of so few rows is fast. `NX_SEARCH_PCI=0` stops the engine from building and dropping these indexes. The engine still uses the indexes that it already built. The result is speed: on the managed service, a search through such an index takes tens of milliseconds, not most of a second.

Since 7.97.0 (engine 0.1.181), the engine also checks its own results. An approximate index can drift as its collection changes. So, for each indexed collection, the engine keeps a small set of recent query vectors. It compares the answers of each index with an exact scan, and it marks the index green, red or unmeasured. `GET /v1/status` shows the counts. The engine reports a red index but does not rebuild it. A red index tells you to reindex that collection.

## Document metadata comes from the catalog

A search hit now gets its title, path, tags and bibliographic fields from the document that owns it. Nexus stores identical chunk text only once. Before, a chunk that two documents shared showed the document that wrote it last. In 7.90.0, search and `query` stopped reading the file path from a chunk row. From 7.91.0, the catalog answers a `where` filter on the tags or category of a document, for each owning document. 7.95.0 added this to the metadata, aspect and graph searches and to `get`. From 8.0.0, the client writes only the chunk keys on each chunk row, and the expiry of a note comes from its catalog entry. The result: a filter on a title, a tag or a year finds the documents that have it, and a hit names the correct document.

## Search and answers

Since 7.89.0, `nx_answer` tells you when the evidence for the answer is thin. Before it answers, `nx_answer` checks if the retrieved evidence covers each part of the question. A partial answer carries an `[insufficient-evidence: <n> unresolved]` notice, so it does not look complete. The check adds some model calls. To turn the check off, set the environment variable `NX_ANSWER_COVERAGE=0`.

From 7.97.0, a question that lists entities gets a multi-step query plan. An example is "which companies were affected". Before, such a question got one query with no coverage check, and the answer could lose a name that the evidence had. Now the builtin `entity-resolution` plan searches, follows links, ranks, and writes the answer. Then the coverage check applies. This takes much longer than one query. You can also map the documents of a collection to entity keys (`entity.collections`, from 7.95.0). Then a plan can resolve those keys to documents by exact title. From 7.97.0, the coverage check also uses this mapping to repair a missing part, by default. A test on the HERB set named every company correctly. One case has no measured precision yet: an answer written from a join that the coverage check did not confirm. If you do not set `entity.collections`, nothing about keys changes.

Since 7.89.0, MinerU is the PDF extractor. It is the default for `nx index pdf`, `nx dt index` and `nx dt capture`. Docling, the earlier extractor, and its dependency are removed. The old option values still work: they run MinerU and print one line that says so.

Engine 0.1.185 (9 November) skips the slow half of the text gate for long lexical queries. It is live on the managed service, and local installs get it from 8.1.0, which pins engine 0.1.186. The text gate in front of hybrid search ran a trigram match next to the full-text match. On 35 sentence-length questions checked locally, the trigram half let no rows through, and it cost 20 to 30 times as much as the full-text half. So a gate text of more than eight words now skips the trigram half. The cost: above eight words, a pasted passage with one changed word, or an identifier inside a longer sentence, no longer matches through trigrams.

Engine 0.1.187 (8.1.2) fixes hybrid search that came back empty or short. This happened when a selective text filter matched a large share of a collection, and the matching chunks ranked behind many closer chunks that the filter excluded. The engine now ranks the filtered set exactly.

## Sensitive documents

Since 7.96.0, you can mark a document as sensitive. Nexus then keeps that document out of what it sends to the LLM or to an outside embedding service. You set the flag when you add a document: use `--sensitive` with `nx index pdf`, `nx index md`, `nx dt index`, `nx dt capture` or `nx store put`. You can also set it later with `nx catalog sensitive`. By default, a local install embeds a sensitive document with the local model. Only `--ack-external-embedder` sends it to Voyage. In cloud mode, the CLI requires that flag, and first tells you what will leave the machine. The MCP `store_put` tool cannot acknowledge, so it refuses. Nexus records every change of the flag, with its reason and the person who made it.

Since 7.98.0, reads also obey the flag. A read that sends data to a model, such as `nx_answer` or taxonomy discovery, asks the engine to remove flagged chunks inside the read. On an engine that cannot do this, the read refuses. By default, the MCP read tools do not give flagged hits to the model of the session. `nx dt index` copies the DEVONthink AI exclusion setting into the flag. The flag has known gaps, listed in the sensitivity guide. Read the guide before you depend on the flag.

## Install and platforms

Since 7.81.0, the plugin installs the nx command line for you. The MCP server of the conexus plugin has a `setup` tool. Say "Set up Nexus", and it installs the CLI, in local mode or CLI-only. `nx doctor` then reports the result of the setup. For the managed service, setup opens a credentials page on your own machine, so the token never goes through the model. Since 7.83.0, the plugin is also the recommended way to install Nexus in Claude Desktop. The older desktop extension does not update itself. Its notice tells you how to change to the plugin.

Nexus now runs natively on Windows x64. 7.72.0 installs and runs the local engine: the engine, the bundled PostgreSQL 17 with pgvector, and the CLI. You do not need WSL. 7.72.1 fixes a hang in MCP search on Windows. The executables are not code-signed yet. Windows on ARM and 32-bit Windows refuse to install. `nx init` checks Smart App Control before it provisions, and `nx doctor` reports it (7.81.0). The Windows guide lists the supported versions.

## Upgrades that keep their work

The first upgrades to 8.0.0 on large local stores wrote to disk heavily for 20 minutes or more, with nothing on screen. The cause was the per-collection indexes from 7.77.0. Every engine restart ended the index build in progress, and the next start built that index again from zero. A store that restarted often never finished its queue of builds. Conexus 8.1.0, 8.1.1 and 8.1.2 fixed this on 10 November, with engines 0.1.186 and 0.1.187.

Since engine 0.1.186 (8.1.0), a stop or a restart that leaves Postgres running does not end the build. The next engine adopts the finished index. A failed build is rebuilt in the same pass, not ten minutes later. Each build also gets more memory, up to 1 GB on the bundled Postgres, so it runs faster and a large build writes less to disk. In a local test, a 40,000-row build took about 35 seconds instead of about 125, and an 80,000-row build wrote at most 1.2 GB instead of about 3.2. `NX_SEARCH_PCI_BUILD_MEM_MB` sets a different value. Engine 0.1.187 (8.1.2) also keeps the build across a hard kill of the engine. Its `GET /v1/status` lists the heavy work the engine is doing and whether it is safe to stop. A reboot, or a stop with `--with-pg`, still stops Postgres and ends the build, and the next pass builds that index again.

The client now says what it is doing. From 8.1.0, `nx upgrade`, `nx self install` and `nx doctor` print each step and the time it has run, and a download shows its size, rate and time left. A command that waits on a migrating engine says so. `nx doctor` no longer reads the whole catalog to find pending upgrade steps, so on a large store it takes about 15 seconds, not 3 to 4 minutes. A new Claude session no longer restarts a healthy storage service. In 8.1.1, the data steps of `nx upgrade` from a 7.x store read the catalog fewer times. On a 140,000-row store they take about 3 minutes, not about 9. In 8.1.2, when the engine restarts during a data step of `nx upgrade`, the step waits for the engine and then continues.

From 8.1.2, `nx backup` writes a copy of the local database to one file while the engine keeps serving. Nothing runs it for you: not `nx upgrade`, not `nx doctor`, not a hook.

Two new release checks protect the next upgrade. An engine release fails if the first boot after an upgrade, or a plain reboot, writes more than 64 MiB. A client release runs `nx upgrade` on a store of realistic size, and fails if the output stops for more than 10 seconds or a step runs past its time limit.

## Agents: the tuple space and waiting for CI

Since 7.41.0, agents have a shared place where they leave messages for each other. This place is the tuple space. It holds small records: one session writes a record, and another session claims it. Sessions use it to give each other work and mail. Later releases added claim renewal and replies inside an acknowledgement (7.44.0), size limits (7.45.0), and boards, queues, locks and a verb to give a claim back (7.51.0). A session that started without the channel flag, for example in the Claude Desktop Code tab, can run `nx tuple wait` as a background task. The session continues when its mail arrives (7.90.0). From 8.0.0, `nx ci wait <sha>` waits on the CI board for the runs of that commit. It prints one line with the result of each run, and its exit code tells a script the result. An adapter that you host yourself fills the board from GitHub webhooks (RDR-220). So an agent can wait for a peer, or for CI where the board has data, without asking every minute.

## Agents: decisions for a person

Some decisions only a person can make. A session writes such a decision as an ask: the question, the options, and whether each option can be undone. Sessions that follow the conexus `asks` skill keep a status entry on a shared board, and the ask goes in that entry. `nx console` shows the open work of every session, with the asks first (7.86.2). Since 7.97.0, `nx ask list`, `show`, `answer` and `digest` let you answer from the terminal. Your answer goes back to the session that asked. So you can answer many sessions from one place.

## Agents: small programs in a sandbox

Since 7.99.0, agents can run small programs in a sandbox. `nx run NAME` and the MCP tool `procedure_run` run a library procedure. The procedure is Lua 5.4 in a separate worker with an empty environment. Nexus checks it before it runs, and limits its instructions, memory, time and result size. A four-line header declares the arguments of the procedure and what it can read. In 8.0.0, two record-keeping steps of the agents use procedures. The agent gives the facts, the procedure returns the record in one fixed shape, and the agent stores it. A later search can trust records that all have one shape.

## Agents: keeping their place across compaction

When the context of a long session fills, Claude Code compacts it: it replaces the conversation with a summary. On one session, two automatic compactions at about 969,000 tokens gave summaries of about 0.5 percent of the context. The state that the session needed to continue was in the repository, the beads and its board entry, not in the summary. Since 7.91.1, the conexus plugin writes that state down before a compaction and puts it back after it. Before the compaction, a hook writes a checkpoint. It holds the session's entry on the shared board first, then its worktree, branch and HEAD, the beads it holds, its uncommitted files, its open pull requests and its recent history. After the compaction, a hook puts the checkpoint into the new context, and the summary is added to the same file.

Claude Code also compacts a session that has been idle for about an hour. Since Claude Code 2.1.314, the summary of that idle compaction can be used at the next message, possibly hours later. Since 7.94.0, the hooks match a checkpoint to its compaction by use, not by age. From 8.1.2, a checkpoint that no compaction has used is kept for seven days. Before, another session's hook could delete it after two days, and the idle session then had nothing to restore. The session that coordinated the 8.1 releases compacted several times, and each time it continued from its checkpoint.

## If you are upgrading from 7.6.x

`nx upgrade` does most of the work. It updates the package, moves the engine to the pinned version of the release, and updates the database roles and grants, the running service, the plugin and the plan library. Then it runs the data steps that fill the new catalog fields. You can safely repeat every step. The engine migrates its own schema on the first boot. The service changes the local cluster to password authentication on its next start.

A few things may need you:

- **Free disk, on a local install.** The first start of the 7.73.0 engine rewrites every vector in one transaction and cannot be undone. Check that the disk holding the Postgres data has free space of at least 2.2 times the vector tables, and reindex or delete any collection whose vectors do not match its model's width, before you upgrade. Either problem stops the migration, and the engine stays stopped.
- **A local cluster older than PostgreSQL 17.** The engine will not start on it. Copy the data directory, run `nx daemon service pg-upgrade --old-bin <bin dir>`, read the dry run, and run it again with `--apply`. If you do not need the data, move the directory aside and run `nx init`.
- **The managed service.** Run `nx upgrade` once yourself, from one machine, so the data steps that the automatic upgrade skips run for your tenant.
- **Note expiry.** 8.0.0 reads a note's expiry from its catalog entry, so some lapsed notes become due. With the 8.0.0 client, `nx catalog backfill-meta --list-ttl-first-sign` is a dry run that lists the lapsed notes the first expiry sweep under the new rule would retire. Read the list before that sweep runs.
- **A backup, if you want one.** From 8.1.2, run `nx backup` before you upgrade. Nothing runs it for you.
- **The desktop extension.** If you installed Nexus in Claude Desktop as the `.mcpb` extension, switch to the conexus plugin and remove the extension.
- **A self-hosted engine role with BYPASSRLS that is not a superuser.** The engine refuses it from 7.95.0. Run `ALTER ROLE <role> NOBYPASSRLS` as a superuser, then restart.

One thing `nx upgrade` does not repair: 7.6.1 stored some `nx store put` and MCP knowledge writes without a catalog entry. 7.7.0 fixed new writes. For the earlier ones, `nx catalog orphan-backfill synthetic <collection>` registers catalog entries for chunks that have none.

Then run `nx doctor`. No row should show as failed. The migration runbook has the detail for each step.

## Further reading

- [Migration runbook](../migration-runbook.md), every upgrade step and how to check it
- [Sensitive documents](../sensitivity.md), what the flag covers and what it does not
- [Backup and restore](../operations/backup-restore.md), what `nx backup` writes and how to restore it
- [Windows install](../windows-install.md), the native Windows path
- [The tuple space](https://hellblazer.github.io/nexus/tuple-space.html) and [Coordination](https://hellblazer.github.io/nexus/coordination.html), how agents wait and hand off work
- [CHANGELOG](../../CHANGELOG.md), every release in this window, with the details this essay leaves out
