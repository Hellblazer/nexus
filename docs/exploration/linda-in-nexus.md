# Linda in Nexus

Nexus borrows one idea from David Gelernter's Linda, the 1985 coordination language, and builds it on the Postgres engine that already serves every other store: a tuple space. A tuple space is a shared bag of typed records that any process can add to, read from, or take from, where a take is atomic. When two processes reach for the same record, exactly one gets it. Gelernter's observation was that this one primitive, applied to records of different shapes, gives you a work queue, a mailbox, a lock, a barrier, and a request with its reply, without designing any of them separately.

To be clear: this is a coordination primitive for the processes that work with you inside Nexus, not a distributed computing platform. We needed three things. An agent's report had to be something an orchestrator could wait for and something a later session could count. A message to an agent working mid-turn had to reach it before it composed its next reply. A request from one Claude Code session to another on the same machine had to be delivered without a person relaying it. Linda's model provided all three in a form that was small, well-studied, and already half-built in our engine. We could have added a mailbox table and a lock table beside the queue we already had, or kept polling memory and scratch. We chose a tuple space because the needs kept recurring in the same few shapes, and one primitive with a registry of shapes is less code than three tables by the second consumer.

This document explains what we took from Linda, what we deliberately left out, how the result fits beside the three stores, and what it changes for you. The full design rationale, its research, and its gate history are in [RDR-205](../rdr/rdr-205-linda-tuple-space-over-postgres.md), and, for the claim renewal and reply-in-ack described below, [RDR-206](../rdr/rdr-206-tuple-claim-renew-and-reply-in-ack.md). The operational reference is [Tuple Space](../tuple-space.md), and the [walkthroughs](../tuple-space-walkthroughs.md) draw each use as a sequence.

## The problem: read-many stores cannot coordinate

Nexus runs many cooperating processes. There is the Claude Code session you talk to, the agents it starts, the hooks that fire around every tool call, `claude -p` subprocesses, and usually several other sessions on the same machine working other repositories against the same engine. They already share plenty of storage. Memory, scratch, the knowledge store, and the file system are all shared, and the harness carries messages between a session and its agents.

Every one of those shared places is read-many. Any number of readers see the same entry, and no reader can claim it. That is exactly right for notes, findings, and documents, and exactly wrong for coordination. Two agents that both read "take the next task" both take it. A message written for one agent is visible to every agent, and the one it was for does not know it is there until it looks. The harness's own messaging is point-to-point and momentary: it reaches one reader, at a time the harness chooses, and is then gone. Nothing records that a report was owed, so an agent that finishes without reporting leaves no trace. Nothing wakes a reader when a record arrives, so a correction sent mid-turn lands after the turn.

The engine already had the missing operation, in one place. The queue that feeds structured extraction hands each row to exactly one worker with a single statement, `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`. Nothing a session, a hook, or an agent could call had that operation. The tuple space generalizes that statement into a primitive any process can reach, over records whose shapes the engine checks.

This is the role Linda fills in Nexus. Not a parallel programming model, but a coordination substrate that gives the processes around you an exclusive take and a wait, on the same durable, tenant-scoped database that holds everything else.

## How the suite leverages it

Two consumers ship with the design, and the design's own scope clause says a third needs its own RDR.

**The dispatch ledger.** Every agent the harness starts gets a per-instance id, and the hooks that run at agent start and stop write a start tuple and a report tuple to the session's ledger, keyed on that id. The orchestrator waits for a report with a read that parks until the tuple lands. Nothing about this requires the agent's cooperation: the stop hook writes the report from the harness's own payload. A later session can read the ledger of any session in the last ninety days and ask which agents started and never reported, which used to be a loop over per-session files that only a shell function on the same machine could read.

**The mailbox.** Every agent, and every session, has a mailbox addressed by its id or its name. A message is a tuple in it. The agent drains its mailbox with a take before it composes any hand-back, so a directive sent while it works is read before the reply, not after. The same mailbox, addressed to a session name instead of an agent id, carries the request and ack between two Claude Code sessions on one box. Both sessions mint against one tenant, so no new addressing scheme was needed, and the requester parks a take on its own mailbox and wakes when the ack arrives.

**The census.** Because tuples are rows with registered shapes, coordination state is queryable. A read with an empty pattern returns everything live in a subspace. A list by prefix returns every subspace with counts. A claim log that is never edited holds the history of every take, ack, nack, lease end, and dead letter. Three `nx doctor` rows watch the space: the oldest unclaimed tuple per subspace, the health of the table, and the age of the last sweep.

## What we borrowed

### The three operations

Linda named `out` (add), `rd` (read), `in` (take), and `eval` (spawn). We ship the first three, plus their non-blocking probe forms and an `ack`/`nack` pair that closes a take. A read matches by equality on whatever keys the pattern supplies, so an empty pattern reads a whole subspace. A take must give every pinned key exactly. A read with a timeout parks inside the engine and wakes on commit, which is what makes the space a meeting point: one process writes a tuple that means "done", another wakes when it arrives, whichever started first.

Three operations with exact meanings are easier to build correctly, easier to analyze, and easier to compose than a larger interface. Each is one short transaction, so the space scales as the database scales, and a busy subspace does not slow a quiet one. Because a take matches on keys alone and always ends in an ack or a nack, you can say precisely what happens to any tuple under any sequence of calls, including a crash between a take and its ack.

### Tuples as signals about data

A tuple is metadata, not data. It says that an agent started, that a report exists, that a message is waiting, that a request needs an answer. It carries keys, a few dimensions, and a body that points at whatever it is about: a document by catalog address, a memory entry by project and title, a chunk by content hash, a bead by id. There are no tuple documents. The content stays in the store built for it, and the tuple space holds only the signals. This is the same discipline the [catalog](xanadu-in-nexus.md) applies to links: a reference, never a copy. It keeps every tuple small enough to travel the same edge as any other call, it means a message cannot go stale when the original is corrected, and it means a claim in a message can be checked by opening what it names.

### Registered shapes

Linda's tuples were untyped. Ours are not. A subspace is a named partition of the space, and every subspace kind has a template registered in the engine: which keys are required, which dimensions are allowed, how long a tuple lives, whether takes are allowed, and how many failed takes turn a tuple into a dead letter. The engine checks every write against its template and refuses one that does not fit before it stores anything. Only the engine holds the registry, and a client can ask for it and its digest, so a hook or a skill that expects a shape detects skew instead of guessing. Two templates exist: the ledger, where takes are disabled and rows live ninety days, and the mailbox, where takes are enabled, a claim holds at most fifteen minutes, three failures dead-letter the row, and rows live seven days.

### Leases and the claim log

JavaSpaces, the Jini-era Linda, added leases. So do we. A take holds a claim under a lease, and a reader that crashes loses its claim when the lease lapses, so the next reader gets the tuple and the sweep releases what nobody retook. Every transition writes a row to an append-only claim log, which is the only history: a restore from backup keeps the log and re-earns any live claim by lease lapse. A dead-lettered tuple leaves every claimant's view but stays readable, so a message that crashes its readers is visible instead of lost.

### Idempotent writes

A tuple's identity is computed from fields the caller supplies, never from the insert time. A retried `out`, across a dropped connection or an engine restart, is the same tuple. This is what lets a client retry through a deploy gap with no coordination protocol of its own, and it is why a resent message lands on the tuple that was already consumed instead of being delivered twice.

## What we left out

**No `eval`.** Linda's fourth operation spawned a process to compute a tuple. The harness spawns agents; the space does not.

**No wildcard or subset matching on a take.** Linda matched a take against any tuple whose fields fit the pattern, with nulls as wildcards. A lock or a mailbox must never be taken by a pattern less specific than the tuple, so our take requires every pinned key by equality. Subset matching stays on the read, where a wide pattern costs a read and not a claim.

**No semantic take.** The first version of this design, in May 2026, matched takes by embedding similarity over a vector store, with a per-subspace floor. It was scrapped with its substrate, and the calibration it earned does not transfer. Similarity, if it ever returns, goes on the non-destructive read only. The vector column is named in the design and not built.

**Renew and reply-in-ack, closed by RDR-206.** The first version shipped with a claim that could not be extended, so a mailbox claim lasted at most fifteen minutes, safe by scope rather than by construction: nothing at the time held a claim longer. `renew` closes that: a holder still working extends its own claim before the lease lapses, clamped to the tuple's own expiry and refused above the template's cap, so it stays a deliberate act by the holder rather than a raised ceiling. The first version also wrote a reply as a separate `out` after the take that claimed the request, so a reader that crashed between the two left the request re-delivered and, worse, the reply already sent, and the requester could see two replies to one request. `ack` now takes an optional `reply`, written and the request consumed in one transaction, so the two either both happen or neither does.

**No third consumer.** The build lease, the session identity files, the push guard, any wrapping of scratch, memory, or plans, and rendered surfaces are all excluded by name. The build lease must work with the engine down. The identity files hold the credential a reader needs before it can read a tuple. The stores are read-many and should stay so. Any consumer not named in RDR-205 needs its own RDR, and that clause, written into the design rather than left to discipline, is the one structural difference between this attempt and the one that was scrapped.

**No federation and no `LISTEN`/`NOTIFY`.** One engine JVM commits every write, so a waiting read parks on an in-process condition per subspace, signalled after commit and on shutdown. The database's own notification channel is unnecessary while one JVM exists and unavailable through a transaction-mode pooler. When a second JVM exists, that changes.

## What it changes for you

Most of the time you touch none of this. Hooks write the ledger, agents drain their mailboxes, and Claude calls the tuple tools when you ask it to wait for an agent, tell an agent something, or ask another session for work. What changes is what you can rely on. A report is owed until a report tuple exists, whether or not the agent remembered. A message reaches exactly one reader, whether or not two raced for it. A request stays open, with an age you can see, until its ack exists, whether or not anyone is watching. Coordination between the processes around you moves from a set of conventions to a set of guarantees, on the same database, under the same tenant, with the same credential.

The public page, [The Nexus Tuple Space](https://hellblazer.github.io/nexus/tuple-space.html), shows the three things you say and what you see when you do.

## Further reading

- [Tuple Space](../tuple-space.md) — operations, templates, a row's life, leases, blocking reads, the sweep, errors, and the JavaSpaces comparison
- [Tuple Space Walkthroughs](../tuple-space-walkthroughs.md) — ledger, mailbox, crash and dead letter, cross-instance request and ack, drawn as sequences
- [RDR-205: Linda Tuple Space over Postgres](../rdr/rdr-205-linda-tuple-space-over-postgres.md) — the problem, the research, the alternatives, and the gate history
- [RDR-206: Tuple Space Claim Renewal and Reply-in-Ack](../rdr/rdr-206-tuple-claim-renew-and-reply-in-ack.md) — closes the two gaps named above
- [Xanadu in Nexus](xanadu-in-nexus.md) — the catalog's linking substrate, which the tuple space points into
- Gelernter, D. "Generative communication in Linda." ACM Transactions on Programming Languages and Systems 7(1), 1985.
