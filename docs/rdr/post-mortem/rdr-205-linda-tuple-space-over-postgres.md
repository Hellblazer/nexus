# Post-Mortem: RDR-205 Linda Tuple Space over Postgres

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

One engine-owned table, `nexus.tuples`, with a tenant-scoped claim log and a
sweep cursor table; ten operations over `/v1/tuples` (out, rd, rdp, in, inp,
ack, nack, registry, subspace_list, subspace_stats); templates loaded from the
engine's resources (two in v1: `ledger/<session_id>` and `mailbox/<address>`);
a Python client, eight MCP tools, an `nx tuple` verb and three doctor rows; and
two consumers: the RDR-184 dispatch ledger projected from the SubagentStart and
SubagentStop hooks, and a mailbox for agents and instances, the same template
under two address kinds.

## Implementation Status

**Implemented, all six phases, and shipped.** Engine half in
engine-service-v0.1.114 (tagged on a2801dfc9, deployed 2026-09-11 01:47Z).
Client half in conexus 7.41.0 (2026-09-11 04:28Z). The ledger projection hook
was dead on every installed box in 7.41.0 and was repaired in conexus 7.41.1
(2026-09-11 11:08Z). Epic nexus-em75s: every phase bead closed; follow-on
beads listed below. Closed 2026-09-11.

## What Differed From the Plan

- **Consumer one shipped inert on every install class.** The projection hook
  (`tuple_ledger_project.py`) resolved the engine only through a local
  supervisor lease or environment variables, so a managed-service box found no
  endpoint (nexus-0zsmg); and it presented only a data-token lease, which a
  local install never holds, so a local box found no credential (nexus-g2lln).
  The hook logged one named skip per dispatch and nothing reached the space.
  Both fixes are in one file and shipped in 7.41.1.
- **The MVV that closed Phase 4 was a fixture.** Run 1 drove the hook scripts
  by subprocess against a dev jar with a minted lease, which is the one
  environment where the hook worked. The Phase 4 critic named the substitution;
  it was recorded as a decision for the author and the phase closed. A real
  dispatch on an installed box, twenty minutes of work, found both bugs the
  same day the release shipped.
- **The plugin-only channel delivered nothing.** The first fix went out as
  `plugin-v7.41.0-1` (RDR-197). Claude Code keys the installed plugin on its
  version field, so a tag move with the same version leaves the cached files in
  place. RDR-197's own Validation section had recorded that pickup as a gap
  never exercised; nobody read it before betting the hotfix on it
  (nexus-konsk). Two hours were lost; 7.41.1 carried the fix by moving the
  version.
- **The engine cut's acquire gate was red three times on client rows.** Every
  engine check was green each time; the doctor's MCP probe budget was marginal
  under load in the container, and `nx init` raced its own lease (nexus-jw44t).
  Both were client bugs, both fixed before 7.41.1, and the fourth run passed
  under the heaviest load of the day.
- **Run 2 measured a pre-fix engine.** The five engine-side targets were set
  from a build that predated two later sweep and claim-log fixes, and the bloat
  leg had stopped at the wall clock, not the million cycles the RDR names. Run
  2b on the tagged tree reached 1,000,031 cycles and every target held.
- **The relay sweep had never been run.** Its space arm was built in Phase 6;
  the first real run surfaced an inbound conexus request twelve days old with
  no acknowledgement (nexus-abyi9). The sweep worked; nothing scheduled it.

## What To Check First Next Time

1. **A hook is proven on an installed box of each class, not in a checkout.**
   Before a tag, drive the shipped hook wrappers on a scrubbed local install
   and on a cloud-mode box and read the artifact back. The fresh-install MVV's
   leg 8d and the cloud gate's leg G now do exactly this; after publish,
   `tests/e2e/post-publish-dispatch-check.sh <session>` proves a real dispatch
   reached the space.
2. **A test that hand-writes the input it then reads proves nothing about a
   box without that input.** Every projector test wrote the data-token lease
   the projector read. Put the absent-input case in the test list first.
3. **A named residual from a reviewer is a red gate until it is run.** Read
   "this is a fixture" as "not tested". Do not file it as a decision.
4. **Read an RDR's Validation section before using the channel it describes.**
   RDR-197 said its pickup path was unverified. A same-version plugin tag
   reaches nobody until nexus-konsk is fixed; ship hook fixes as a client
   version.
5. **Run the sweeps.** Machinery that only runs when someone remembers is
   dead wire. Schedule the relay sweep and the dispatch check, or wire them
   into the release checklist where they now sit (step 11d).

## Follow-on beads open at close

nexus-em75s.39 (eight Phase 1 Test Plan scenarios with no test), .40 (Java
pins), .42 (projector residuals), nexus-f1pbh (diagnostics role has no SELECT
on the three tuple tables; next engine tag), nexus-aginu (one shared endpoint
resolver for the hook scripts; also the stop-payload skip lines), nexus-w5gma
(phase-review-gate parses nothing on this RDR's layout), nexus-zn9op (census
test under box load), nexus-e00lh and nexus-mvfm9 (polish), nexus-konsk (the
plugin-only channel), nexus-joqdj (wire-ledger timing; resolved by the 7.41.0
release, left for the author to close). The "claimable" wording amendment at
RDR lines 911 to 913 is the author's.

## Records

T2, project nexus: phase1- through phase6-close-nexus-em75s-2026-09-1x,
engine-v0.1.114-pretag-2026-09-10, engine-v0.1.114-acquire-runs-2026-09-11,
engine-v0.1.114-deployed-2026-09-11, release-7.41.0-ship-2026-09-11,
release-7.41.1-ship-2026-09-11, shakeout-7.41.0-ledger-projector-dead-on-cloud-2026-09-11,
shakeout-7.41.0-projector-local-install-proof-2026-09-11,
shakeout-7.41.0-cross-instance-mailbox-2026-09-11,
shakeout-7.41.1-real-dispatch-check-2026-09-11, plugin-v7.41.0-1-cut-2026-09-11.
T2, project nexus_rdr: 205-research-1 through 205-research-15.
