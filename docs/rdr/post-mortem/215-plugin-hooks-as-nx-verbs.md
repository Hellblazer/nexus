# RDR-215 post-mortem: retiring the bash hook layer

## What the RDR set out to do

Both plugins ran their Claude Code hooks as bash scripts — about 4,200
lines of them, mostly wrappers around Python that did the actual work.
The RDR moved that layer into the wheel: hooks become MCP tools on
`nx-mcp` or exec-form `nx-hook` verbs, and the bash goes away.

## Implementation status

Implemented. 31 beads, four phases, all closed. Four gates green at
close: unit suite 20787 passed / 0 failed, lint bucket 1273 passed,
`local-service-gate.sh` 597 passed, `plugin-lockstep-gate.sh` passed.

## What diverged

**The tool tier is 13 entries, not the 15 the plan named.** Four hooks
were assigned to the tool tier; two of them never moved.
`mailbox_drain.py` and `routing/subagent_git_write_requires_orchestrator.py`
both reach `_endpoint_resolve.py`, directly or through `routing/_lib.py`.
That module cannot leave `conexus/hooks/scripts/` while `t2_prefix_scan.py`
and `tuple_ledger_project.py` import it, and neither of those is ported by
this RDR. Moving them would have meant a second copy of a 449-line
resolver beside `nexus.db.service_endpoint`, which the RDR's own "move,
do not rewrite" rule forbids. Five conexus hooks stayed plugin-resident
on bare `python3` for the same reason.

That reason was wrong (corrected 2026-09-23, nexus-t9klx). A wheel module
does not need the mirror: it can call the client's own primitives, as
`tuple_ledger_project` already did when this RDR ported it. All five are
now `nx-hook` verbs. The tier rulings stand on reasons of their own; the
RDR's Revision History carries them.

*Drift: missing cross-cutting concern.* The dependency direction — what
may live in the wheel, what must stay in the plugin, and which way
imports are allowed to point — is a system-wide constraint the design
never stated. Every tier assignment was then made without it. Classifying
this as a per-hook oversight would miss that all four exceptions share one
unstated rule.

**`phase_review_close_requires_gate` moved to the command tier.** It is
the routing framework's only `fail_closed: true` rule, and its contract is
that a crash still emits a deny envelope. The tool boundary returns a
raised exception as empty text with `isError` false, and Claude Code reads
a disconnected server as non-blocking, so on that tier both a crash and a
down server would have read as *allow* — a phase could close without its
gate.

*Drift: missing failure mode.* The design reasoned about the happy path
of the tool tier and not about what its error channel does to a gate whose
whole value is failing closed.

**Approach item 8 undercounted its exec targets by one**, and the
Technical Design's promised hooks.json shape lint was never authored until
Phase 4.

*Drift: scope underestimation*, twice, in enumerations the document stated
as complete.

**The stdio integration test drove a placeholder for the epic's entire
life**, and the Contracts section claimed the close gate's deny text was
quoted in 19 files when it was quoted in one.

*Drift: unvalidated assumption.* Both were asserted in the document and
never measured. The second is not merely wrong but inverted: scarcity is
the argument for pinning that text, so the sentence argued against its own
conclusion.

**`nx catalog sync` was deleted rather than moved to a daemon thread** as
the Technical Design specified. It had raised unconditionally since
conexus 7.0.0 and the substrate it synced was removed at RDR-158.

*Drift: over-specified code.* The design named a mechanism for work that
had been dead for two releases.

## The finding worth keeping

The port is unremarkable. What this RDR produced that outlives it is a
tally of fifteen instances of one defect class, recorded at
`nexus_rdr/215-gates-that-lost-their-domain-tally`:

> A refactor that changes how something is SPELLED, while leaving what it
> DOES alone, removes it from the domain of every checker that matched on
> the spelling. The checker does not fail. It finds less, and finding less
> looks like improvement.

Three things in it matter more than the count.

**Four of the fifteen were written during a fix round**, by the person
fixing the defect, in the instrument built to catch it. That is the
largest category. It grew again in the bead whose brief named the risk,
and once more in the lint written specifically to close instance 13. The
instrument is written in a hurry, by the same hand, under the same
assumption that produced the defect.

**Three detection methods emerged, and they find disjoint sets.** Sweep
the checkers for a domain that moved. Cross-walk the promises for a check
that was never authored. Compare each present check's age against what it
claims to cover. A close that runs only the first finds neither of the
others — instance 13 was an absence with nothing to examine, and instance
14 was a file whose text still matched its promise word for word while its
subject had gone stale six months earlier.

**Instance 15 is outside this RDR's code entirely.** A retrieval
benchmark sorted by `r.distance` after a commit moved the quantity it
measured into a different field. It passed at 0.9013 against a 0.9078
baseline, comfortably inside its band, while measuring nothing. A class
that only ever appears in the code of the effort that named it is
plausibly an artifact of that effort's habits; one that turns up in a
benchmark written months earlier by unrelated work is a property of how
checks and the quantities they measure drift apart.

## What to do differently

State the dependency direction before assigning anything to a tier. Both
the tool/command split and the wheel/plugin split were decided per-item,
and the exceptions were discovered one at a time over three phases.

Measure the claims a design makes about the codebase. "Quoted in 19
files" and "these four become tools" are both checkable in seconds, and
both were wrong.

When you write a gate, write the assert that proves it can fail. Every
instance in the tally above was found by something other than the gate
itself — a reviewer asked a specific question, a cross-walk, a timestamp
comparison, or, in instance 15, a non-vacuity assert someone had written
beside the gate for exactly this reason. That assert is the only thing on
develop that noticed a benchmark had gone blind.
