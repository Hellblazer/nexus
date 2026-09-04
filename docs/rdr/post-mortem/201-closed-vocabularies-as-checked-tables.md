# RDR-201 Post-Mortem: Closed Vocabularies as Checked Tables

**Closed** 2026-09-02 · **Accepted** 2026-09-01 · **Arc** nexus-j9z30 (29 beads, all closed)
**Phase gates** Phase 1 PASSED 2026-09-01 · Phase 2 and Phase 3 PASSED 2026-09-02
**Reviews** T2 `nexus/code-review-nexus-j9z30-18-2026-09-02` [24085], `nexus/critique-nexus-j9z30-18-2026-09-02` [24084], `nexus/code-review-nexus-j9z30-23-2026-09-02` [24088], `nexus/critique-nexus-j9z30-23-2026-09-02` [24089]

## What the RDR set out to do

Three closed vocabularies in this repo were maintained as prose and had drifted
from the code that enforced them. A *closed vocabulary* is a small fixed set of
allowed values — the six RDR statuses, the release gate's verdicts — where any
value outside the set is a defect rather than a new case. RDR-201's claim was
that such a set belongs in a checked table (data a program validates) rather
than in a list of literals a person maintains by hand.

Three tables, one loader, one checker, one evaluator:

- **`rdr-lifecycle`** (packaged, 14 rows) — the six statuses and their legal
  transitions. `nx rdr set-status` resolves the table and refuses an illegal
  flip with a typed reason instead of writing whatever it was asked for.
- **`release-choreography`** (repo-only, 89 rows) — the decision cells of the
  two release gates, so both scripts evaluate one table and cannot disagree.
- **`rdr-dependencies`** — not a transition table but the edge list Gap 3
  needed: a status change on one record marks the records that depend on it.

## What shipped

All three phases, and the checker caught real defects at each one.

**Phase 1** landed the table format, loader, checker and evaluator, and derived
every status list in the codebase from the lifecycle table. The status domain
was ruled by Sam to six values; `scrapped` merged into `abandoned`. Review added
three findings to the checker itself (unmatched-assignment, unused-dimension,
and a closed-by-escape advisory that never fired for zero-guard groups).

**Phase 2** enumerated the release scripts' 89 reachable decision cells as a
fixture, authored the table, routed both scripts through one shared evaluator,
proved old-path and new-path agreement cell by cell — on the verdict *and* on
the printed text — then deleted the old path. Two Sam rulings landed here: the
event column came out (event-invariance is correct; the event-sensitivity lives
in the release skills, not in these scripts' inputs), and the O2 order asymmetry
stays as two rows with the reason recorded at both.

**Phase 3** added a canonical-tumbler rule for RDR documents, seeded
RDR-to-RDR edges from frontmatter, and made `set-status` mark the records a
flipped record supersedes. Sam ruled the walk to `supersedes` edges only —
259 of 265 proposed edges are `relates`, derived from a free-text reading aid —
and, at close, to one direction: a successor's flip marks its predecessors.

## What we would check first next time

**A rule proven below the layer that uses it is not proven.** This arc found
the same shape four times, and paid for it twice more after the RDR itself was
written:

- The parity harness compared exit codes and classifier markers and passed
  green while 23 message texts differed — six of which had invented an
  exception paraphrase where the real exception belonged.
- `rdr_hook.py`'s file filter matched zero of this repo's `rdr-NNN-*.md` files,
  so a whole SessionStart reconciler had never executed once, on any session,
  since it was written.
- `tests/conftest.py`'s benign-append classifier was handed `"MODIFIED path"`
  and looked the whole string up as a path. It matched nothing, so every change
  was called a state mutation — for the guard's entire life. Eleven tests
  covered that classifier. All eleven fed it bare paths.

In each case a test existed, passed, and asked a narrower question than the
production caller asks. The check that works is to feed the consumer *the
producer's own output*, not a literal that resembles it. Where that is
expensive, spend it anyway on the seam: the composition test that finally
caught the last one is four lines long.

**A declared-but-unread column is not documentation, it is a claim.** P2.3
declared `event` and `mode` table-wide because an incident write-up said the
incident was an un-encoded event dimension. No row ever guarded on them. The
checker's unused-dimension advisory said so on every run and nobody acted,
because an advisory is not a verdict. When Sam re-read the incident, the fix
commit had changed a remedy string — the event-sensitivity was real but lived
somewhere else entirely. Check what the fix actually changed before encoding a
dimension the incident report asserts.

**Deleting a never-run writer beats repairing it.** `rdr_hook.py`'s reconciler
would have resolved nine known file-vs-T2 disagreements on its first live run,
by a ranking rule nobody had ever watched work. The ruling deleted it and kept
the read-only half. But the ruling's *stated reason* — that `set-status` writes
file and T2 together — was wrong: `set-status` wrote the file and the README,
and the lifecycle skills wrote T2 in prose. The deletion was still right on its
other ground, and the gap it left is now covered by detection (`rdr-audit`
prints one `DRIFT:` line per disagreement) rather than by a writer nobody
watches. Two lessons: verify a rationale before it becomes the record, and
prefer a detector to an unattended arbiter.

**Report the measurement even when it refutes you.** Finding 2 predicted the
release scripts would shrink by about 120 lines net. Measured like-for-like at
close: the two scripts shrank 52 lines while the table (855), the message
catalog (554) and the shared evaluator (155) added about 1,500. The
auditability case stands on its own — 89 scattered branches became 89 rows a
checker proves total and disjoint — but the size claim did not, and the
Trade-offs section was corrected rather than left flattering.

## Residuals, carried deliberately

- **nexus-wjkc7** — `index.log` sits in both the allowlist and the append-only
  set, so its append-only classification is unreachable; and the entry contract
  still accepts either a bare path or a verb-prefixed one, which is the
  tolerance that hid the last defect.
- **nexus-ph718** — `collapse_rdr_registrations.py --apply` has never run live
  and nothing depends on it. Run it or delete it; do not leave it unowned.
- The `needs-reexamination` markers have no automatic resolution path. A marker
  is cleared by hand once the record has actually been re-examined. Whether
  that is enough is a question for the first person who reads a long one.
- `_RDR_SEED_LOOKUP_CAP = 4` is an unmeasured constant with its trade written
  down. Measure before moving it.

## What outlives the RDR

The lifecycle table is the piece with the most leverage: `nx rdr set-status` can
no longer be talked into an illegal transition, and since this arc it also
mirrors the flip onto T2, so the drift class that produced the nine rows cannot
silently re-accumulate. The release table's value is narrower but sharper —
the two gates evaluate one table, so the O1 class (the two scripts returning
opposite verdicts on the same ledger) is impossible by construction rather than
by two authors remembering to keep their branches in step.

## Addendum, 2026-09-04

The lesson above, a rule proven below the layer that uses it, held one more
time after this file was written. The RDR's Problem Statement said a table
would have reported both justifying incidents; nothing asserted where their
cells were until the intrastate reanalysis (T3
`analysis-deep-intrastate-vs-conexus-reanalysis-2026-09-04`) asked. GH #1402
now resolves to a named refusal in `tests/tables/test_release_incidents.py`;
the inversion's failure class is pinned, the incident is not, and the RDR
says why. Two more things the same pass corrected: the lifecycle table
header claimed an omitted status went uncaught (the totality check catches
it), and the checker's guard-independence assumption was undocumented on
both sides of the borrowing. Amendments in the RDR's Revision History.
