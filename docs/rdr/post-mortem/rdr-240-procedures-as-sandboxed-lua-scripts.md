# RDR-240 post-mortem: procedures as sandboxed Lua scripts

## What the RDR set out to do

Fixed procedures (reading session transcripts, writing a gate record, adding
an RDR research finding, the pre-push checklist) lived in memory files that
agents recalled and re-typed each time, so the steps drifted between runs.
The RDR moved them into Lua scripts that run in RDR-242's sandbox, behind
one runner with two doors, `nx run` and the MCP tool `procedure_run`. It
shrank the four memory files to the rules a script cannot carry.

## Implementation status

Implemented. Epic nexus-qo7ie.2 is closed. The shipped library holds
`transcripts`, `gate_record` and `research_add`; `preflight` lives in the
nexus repo's `.nexus/procedures/`. The four memory files went from 15 to
36 KB to 3 to 6 KB each. Both close critiques returned `partial` with 0
Critical findings and 0 ship-blockers; every finding was RDR text that the
code had overtaken, amended in 3baa60593 (T2
`nexus/critique-9b4618611-nexus-qo7ie.2-2026-11-16` and `-r2`). Sam closed
it as Implemented with that override recorded (T2
`nexus_rdr/240-close-override-2026-11-16`).

## What diverged

**The fit line holds on one project only.** A2's pass line (the largest
one-day `transcripts.sessions` result at most half the 20 MiB verb bound)
was measured on the nexus project. On the shipped default, `all`, the busiest
day reaches 14.2 MB. Every result still fits the bound, and Sam kept `all`
as the default (ask q40-all-default, 2026-11-09). The peak Lua memory on that
data roughly doubled between two days of measurement (22.9 to 42.4 MB of
64 MiB) and is not explained.

*Drift: unvalidated assumption.* The spike measured the project in hand, not
the default a user gets.

**One baseline could not be re-derived.** Phase 1 Step 1 promised that
RDR-238's research 2, 4 and 5 could be re-derived from the transcript
queries. Research 2 was; research 5 only in direction, because the `loads`
row has no version field; research 4 not at all, because its counts were
defined differently (188 sessions against 178). RDR-238 Phase 2 takes its
baseline from the query instead.

*Drift: unvalidated assumption.*

**`project` grew a `repo` value.** Worktrees are separate Claude Code project
directories, so a per-project query missed most of a repository's work. The
script added `repo` (every directory of the repository, worktrees included),
which `nx memory curate` now uses.

*Drift: missing cross-cutting concern.* Worktrees were the norm when the RDR
was written.

**`preflight` runs only where the repo root is on.** RDR-242's repo root is
off by default (`NX_PROCEDURES_REPO_ROOT=1` turns it on), and `preflight`
lives there, so it runs in the nexus repo and nowhere else.

*Drift: deferred critical constraint,* settled in RDR-242 after this RDR's
gate.

**Smaller differences.** The destructive-action hook advises and does not
deny; the deny-or-advise call stayed with Sam. The `skills` query ports
`scripts/skill_routing_census.py` and does not replace it. The MCP
description lists shipped procedures only, because reading the repo root at
import would run `git`.

## What is still open

A3, whether agents actually run the scripts instead of improvising, is
measured by the transcripts `procedures` query 30 days after each script
landed. The first reading is due 2026-12-09 and is owned by a thread on the
threads board (session tamsin), with results to T2
`nexus_rdr/240-a3-reading-<date>`.

## Lesson

Measure a fit line on the default a user gets, not on the data at hand.
