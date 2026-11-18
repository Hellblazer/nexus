# RDR-242 post-mortem: a sandboxed Lua runtime over the nexus surface

## What the RDR set out to do

Agents needed to run small, fixed programs over nexus data without the
power to run arbitrary code. The RDR built a client-side Lua 5.4 runtime
(`lupa`) in a separate worker process. It has an empty global environment, a
static check before any run, a closed table of read-only verbs, hard limits
on instructions, memory, time and output, and a run path that takes a
procedure's name from a library and never script source.

## Implementation status

Implemented. Epic nexus-mdbds.1 and its implementation beads (nexus-mdbds.3
to .9) are closed. The worker, kill timer, four-worker cap, static check and
the read verbs (`memory.get`, `memory.list`, `memory.search`, `tables.get`,
`git.diff_names`, `git.diff_stat`, `transcripts.sessions`) are in
`src/nexus/lua_runtime/`. Native Windows was verified on qwent. Both close
critiques returned `partial` with 0 Critical findings and 0 ship-blockers;
the findings were stale text and one test-guard gap, all fixed in the commit before this close
(T2 `nexus/critique-d76add41d-nexus-mdbds.1-2026-11-16`,
`nexus/critique-d1c26b643-nexus-mdbds.1-2026-11-16-r2`). Sam closed it as
Implemented with that override recorded (T2
`nexus_rdr/242-close-override-2026-11-16`).

## What diverged

**Gap 5 closes for the shipped root and is only bounded for the repo root.**
The RDR first said origin alone closed Gap 5. A repo's
`.nexus/procedures/` can be written by any agent in that repo, so a file
there runs unreviewed. Sam made the repo root off by default
(`NX_PROCEDURES_REPO_ROOT` exactly `1` turns it on, 2026-11-08); the nexus
repo turns it on. The residual stands for any repo that does.

*Drift: under-specified architecture.* The approval step that would have
closed it was withdrawn on 2026-11-07, and the text kept the stronger claim.

**The "no run path takes source" guard missed one entry point.**
`supervisor.supervise` takes the script text as the `"source"` key of its
start mapping, which the origin test's parameter-name scan cannot see. The
test now also allows only `run_script`'s own chain to call `supervise`. It
is an accident guard: an import alias would still evade it.

*Drift: missing failure mode.* A scan by parameter name does not see data
passed inside a mapping.

**Tuple verbs were never built.** The RDR deferred the dictionary-described
tuple verbs to the aura stage. That stage (RDR-249) shipped none, because
auras write through host Python. No consumer needs them; a future one opens
its own RDR.

*Drift: scope underestimation of the consumer,* in the other direction: the
expected consumer did not need the feature.

**No approval document is planned.** The RDR deferred write verbs and the
repo-root residual to an approval document. Sam decided on 2026-11-16 that
none is planned: a future write grant starts its own RDR. Version 1 ships no
write verb, and the static check refuses an unknown one.

*Drift: deferred critical constraint.*

## Lesson

When a decision withdraws the mechanism a claim rests on, sweep every site
of that claim the same day. Here the Gap 5 claim, the A1 and A2 status lines
and the tuple-verb pointer each stayed stale until the close critique.
