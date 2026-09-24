# RDR-217 Post-Mortem: A Lexical Leg for the nexus Client

**Closed** 2026-09-24 · **Accepted** 2026-09-19 · **Epic** nexus-lqo4p (21 beads, all closed) · **Shipped** conexus 7.55.0 (2026-09-20), no engine cut

## What the RDR set out to do

The engine keeps two lexical indexes for every chunk it stores: a full-text
index (`chunk_tsv`) and a trigram index over the raw text (trigram matching
compares three-character sequences, so a partial word can match). Only one
engine route reads them, `POST /v1/vectors/hybrid-search`, and before this RDR
nothing in this repository called it. `nx search` was vector-only.

Two gaps followed. Gap 1: a search for an exact identifier or a rare token got
whatever the embedding model judged nearby, while an index that could match
the token itself sat unread. Gap 2: the hybrid route had a production consumer
(the conexus cloud deployment) and no caller in nexus's own test suite, which
had already cost an outage (BUG-0148, recorded in the RDR-180 post-mortem).

## Implementation status

Implemented. `HttpVectorClient.hybrid_search`, a `--lexical` flag on
`nx search`, a `lexical` parameter on the MCP `search` tool, an engine-substrate
journey test, and a recurrence detector for the BUG-0148 shape. Shipped in
conexus 7.55.0. No engine change was needed; the route already existed.

## The three questions this record exists to answer

**Did the lexical leg help?** Yes, and more narrowly than the Problem Statement
implies. Precision@10 (the share of the top ten results that are relevant),
measured on 719 chunks of this repo's code, both routes on the same freshly
built index (T2 `nexus_rdr/217-phase1-report`):

| Query shape | Vector only | Hybrid |
|---|---|---|
| identifier (6 queries) | 0.867 | 0.961 |
| rare token (3 queries) | 0.167 | 0.698 |
| prose (3 queries) | 0.133 | no rows at all |

Identifier search had little headroom: four of the six identifiers scored 1.000
on both routes, and the whole gain came from the two low-frequency ones. The
leg earns its keep on rare exact tokens, where the gain is roughly fourfold and
one query (`word_similarity_threshold`) was invisible to vector search. The
sample is small (three queries per shape for two shapes) and drawn from code
only; the direction is clear because the gaps are large, and no small
difference in these numbers is a finding.

**Was the surface additive or explicit, and did the measurement decide it?** It
decided it. The hybrid route returned zero rows for every prose query, so a
mode that replaced vector results with lexical ones would hand a natural-language
query an empty result with no explanation. The shipped `--lexical` is additive:
lexical rows join the vector rows. Sam had leaned additive before the numbers
existed ("additive, i suppose", T2 `nexus_rdr/217-sam-decisions`); the
measurement supported the lean rather than merely failing to contradict it.

**When did Gap 2 close, and by which test?** At `91bd47bcd` (2026-09-19):
`tests/test_rdr217_hybrid_search_journey.py` drives the route against the
engine substrate. It is unmarked, so it runs in the default CI loop, and CI
sets `NX_T2_SUBSTRATE_EXPECTED=1`, so it cannot silently skip.

## Implementation vs plan

### As planned

- The flag is `--lexical`; `--hybrid` keeps its frecency meaning (Sam, before accept).
- The MCP `search` tool carries the same parameter, so the leg is not CLI-only.
- A lexical row is exempt from the per-collection distance threshold, and
  `--lexical` refuses on a backend without the hybrid route rather than falling
  back (both Sam, after accept).
- The wire body is pinned as an exact seven-field key set.

### Diverged

- **The RDR contradicted itself on Phase 2.** Its Phase 2 step list named two
  steps and said Phase 2 "stops there, and deliberately", while its Minimum
  Viable Validation required the engine-substrate journey in Phase 2 or 4. A
  literal reading would have shipped without the Gap 2 closure. The plan caught
  it and created bead nexus-lqo4p.8; nothing was dropped.
- **The return type narrowed** from `list[dict] | dict` to `list[dict]` (Sam,
  "narrow to list[dict]"): the dict arm was unreachable once `structured` was dropped.
- **The feature did not work in the invocation Phase 1 measured**, and a green
  checklist did not show it. The first Phase 3 commit called the lexical leg
  without rerank; the CLI orders reranked rows first and truncates at n, so every
  lexical row fell off the end of a default `nx search --lexical`. The MCP surface
  never reranks and so did not have the defect: the two surfaces diverged exactly
  where the feature's value lives. Both reviewers found it; fixed at `68bb35469`
  by reranking the lexical leg whenever the vector leg is reranked.
- **`--hybrid`'s help text changed**, against the Phase 3 checklist's literal
  "unchanged in help text". The meaning is unchanged; the text now says it
  re-ranks rather than retrieves and is unrelated to `--lexical`. Recorded as a
  deliberate deviation.
- **The Test Plan's 503 assertion was not written.** A real jar boot always
  wires the vector backend (`Main.java` constructs it unconditionally), so the
  503 path is unreachable from a nexus test; the engine's `VectorHybridHttpTest`
  covers it.

### Planned but not implemented

- **Live recurrence detection.** Phase 4's detector is a CI assertion on a
  fixture with known ground truth. It proves the comparison logic, not that
  production is free of the BUG-0148 shape. Live detection needs a ground-truth
  canary corpus and a cadence to run it on; both are real design this RDR did not
  specify, so they are named here and deliberately not filed. The BUG-0148 risk
  is PARTIAL, not closed.

## Drift classification

| Divergence | Category |
|---|---|
| Phase 2 step list narrower than the MVV | Internal contradiction |
| Identifier gain smaller than the Problem Statement implies | Unvalidated assumption |
| Lexical rows truncated away by CLI rerank ordering; CLI and MCP diverged | Missing failure mode |
| Live BUG-0148 detection left for a canary corpus | Deferred critical constraint |

## What to check first next time

1. **Run the feature in the invocation the measurement used.** Phase 3's close
   gate would have passed on the text alone: the flag, the parameter and the docs
   all existed while the default command dropped every lexical row. Drive the
   surface end to end before calling a phase closed.
2. **When two surfaces share a feature, test both in their default mode.** The
   CLI and MCP paths differ in whether they rerank, and the defect lived in that
   difference.
3. **Check that a test can fail for the reason it names.** The epic produced six
   instances of its own cited failure class (a check whose domain does not contain
   its claim), four found by review and two by the implementer. Among them: a docstring
   pinning a removed copy, a non-vacuity plant that moved the corpus with the
   query, a coverage pointer naming one file of two, a positive control passing
   against a dead route, and a test that inspected a signature while claiming to
   guard a schema. Treat that as a rate: expect roughly one per reviewed commit.
