# RDR-232 Post-Mortem: nx_answer Answer-Shape Eligibility and Evidence Sufficiency

**Drafted** 2026-10-30 for the close · **Accepted** 2026-10-25 · **Epic** nexus-jypmj (every child bead closed at drafting; the follow-ups below are open) · **Shipped** conexus 7.88.0 (containment, the eligibility gate, the run summary; the coverage check opt-in), 7.89.0 (the check on by default with concurrent assessment calls, the engine pre-cap scope filter on the client side, `secondary_omitted`); engine-service-v0.1.166 (run summary), v0.1.168 (scope filter, `out_of_step_cost_usd`), v0.1.170 (`node_walk`). On develop, not in a release at drafting (f0d8dcc9e): the lookup-only row, the unread-anchor rule, repair that reads unread items, and the client use of `node_walk`.

## What the RDR set out to do

`nx_answer` picks a saved retrieval plan before it runs it, then asks a model to write an answer from what the plan retrieved. On 2026-10-23 a request for a synthesis of one design record (ART RDR-084: its architecture, numbers, evidence, gate history, state and open work) picked plan 473, a plan built to answer single-fact questions. The run retrieved 340 chunk references, 339 of them code and one of them the record. The answer named one theme and omitted every other part, and still reported itself as `answered`.

The RDR named three gaps. First, the matcher never asked whether a plan's kind of answer fits the request, so it needed an answer-shape contract (each plan declares `lookup`, `synthesis` or another kind; each request is classified; a mismatch makes the plan ineligible before similarity or cost is compared). Second, a plan-pinned corpus and an unscoped link walk let other evidence crowd out the evidence the caller named, so it needed scope that reaches every retrieval step and a bounded traversal. Third, nothing compared the retrieved evidence with the facets (the separate parts) the caller asked for, so it needed a coverage check before the model writes, with one repair and an honest withhold when a facet stays unsupported.

## Implementation status

Implemented, with one open gap on the incident's own class: a single-source synthesis whose parts sit in different sections still loses sections to the 24-chunk primary cap (nexus-jf9wg), and in three of six such runs the coverage check marked a part covered that the answer said was missing (nexus-dfrfp). All three gaps have shipped code and live measurements: the eligibility gate enforces on every matcher branch; scope reaches plan-pinned searches and bounds the graph walk; the coverage check runs by default, withholds an answer whose facets no read item supports, and persists its summary. The incident request now resolves ART RDR-084, excludes plan 473, and in the 2026-10-30 rerun covered 6 of 6 facets.

Six decisions or measurements changed the design in the building; they are listed below, and Sam confirmed the list on 2026-10-30. Four findings are still open as beads. The evidence is thin in places, and each place says so: most live figures are single requests or samples of 5 to 30, the quality labels were drafted by model readers, and two rollback legs were simulated only.

---

## Implementation vs. plan

### As planned

- The answer-shape table: a closed vocabulary, declared in a validated top-level `plan_json` field and never in `dimensions` (which define plan identity), with legacy plans decoding to `unknown`. Held-out wrong-shape admission fell from 1 of 29 to 0 after the lookup change below, for request kinds extracted by the model (Haiku or Opus); with first-pass (deterministic) kinds it goes from 1 to 2 under every rule, which is why the gate runs in shadow when extraction is switched off.
- Caller scope applied over plan-declared corpora, on the runner and on the `single_query` fast path. On the incident request the vector stream holds RDR chunks only.
- The traversal policy inside `traverse`, before manifest expansion: on the fixture, scope leakage at the reducer fell from 0.40 to 0.0.
- A named primary source resolved by owner or project, with section-diverse selection from manifest order and chunk content, never from stored section titles (those were falsified as document-scoped metadata in research 7).
- A runner-owned boundary for the coverage check, one repair and one terminal reduction, a nullable versioned run summary on the existing table, and `INSUFFICIENT_EVIDENCE` outside the non-answer shapes.

### Diverged (Sam confirmed 2026-10-30)

1. **The engine's pre-cap scope filter dropped in-scope nodes reached through out-of-scope relays.** The RDR planned a scope predicate applied during the graph walk, before the engine's 500-node cut, and described it as a filter on the existing walk with no new algorithm. The filter removes out-of-scope nodes, and a node that is in scope but reachable only through one arrives with no edge, so the client cannot tell its link role or distance (`related`, no distance). On the G06 walk (the incident request) 25 of 30 in-scope documents, 83 percent, were reached that way, and a policy keeping one role dropped them: the engine-filtered run retained 5 where the client-side run retained 30. Fixed on 2026-10-29 by Sam's choice of both a client stopgap and an engine change, `node_walk` (each node's walk depth and best link role), live in engine-service-v0.1.170; the rerun retained 32 with 0 dropped by role. A fixture with a relay node in it would have shown this. *Drift class: missing failure mode.*

2. **The shipped directional lookup rule admitted synthesis plans for lookup requests.** The RDR's table let a `lookup` request use `lookup`, `synthesis` and `unknown` plans. A synthesis plan can outscore the lookup plan, so one held-out lookup (H09) ran citation-traversal at 1.24 USD against 0.14 USD for the lookup plan, a live single-theorem lookup ran a synthesis plan at 1.64 USD, and the one held-out wrong-shape admission could not be removed. After measuring three rules, Sam chose lookup-only on 2026-10-30: held-out wrong-shape admission went from 1 of 29 to 0 with no recall loss (21 of 29 against 20), for model-extracted request kinds (first-pass kinds give 1 to 2 under every rule). The price is that a lookup no lookup plan matches goes to the inline planner (a planner that writes a plan for the request), which cost 0.24 to 0.32 USD on three requests, about 1.3 to 1.7 times the real-traffic median. The control that tests the other side, six one-source requests whose parts sit in different sections, had been written on 2026-10-28 and was not run until after the rule shipped. *Drift class: unvalidated assumption.*

3. **The omitted-anchor rule: a facet the check could not fully read was marked `unassessed`, and `unassessed` cannot withhold.** An anchor is one retrieved item handed to the coverage check, and each assessment call carries a bounded number of them. The RDR said a facet with no supporting evidence is uncovered. As built, a facet was marked `unassessed` whenever a call had left anchors out, and `unassessed` blocks the withhold. Negative control N04 handed the check 301 anchors, 127 were read, and off-topic prose came back `answered`. After the fix (Sam, 2026-10-30, nexus-jypmj.23) a facet no read item supports reads `unsupported`, the notice says how many items were not read, and repair reads the unread anchors. The price: a correct answer whose support sits in the unread tail is withheld. Five capped runs gave 0 false withholds, which estimates nothing; a constructed request had 10 of its 14 supporting items in the unread tail and was saved only by the 4 that were read. *Drift class: missing failure mode.*

4. **Three of the four negative controls were contaminated by the live tenant.** A negative control is a request nothing in the corpus can answer, used to see whether the check covers a facet it should not. The pin that kept the controls honest scanned the repository for the marker terms, not the tenant that the live run searches, so three markers were in the tenant's data. All three were replaced with markers absent from the tenant and run on 2026-10-30: with the check on, N01 to N03 were all withheld (0 of 6 facets covered); with it off, two returned off-topic prose as `answered`. This is a presence check on four requests, not a false-cover rate. *Drift class: unvalidated assumption.*

5. **The engine's scope predicate cannot express an owner.** The RDR (section 3) specified an allowed-collection predicate and section 2a specified owner narrowing for secondary streams, and did not say that an owner is not a set of collections. The predicate takes a set of collections or a subtree. Owner narrowing therefore stays on the client, after the engine's cut. It dropped 0 nodes on 6 owner-bound walks (largest walk 154 nodes, far under 500), so whether it can ever spend cut slots on another owner's documents is unmeasured. *Drift class: under-specified architecture.*

6. **The coverage check was turned on by default on 2026-10-28, ahead of the planned opt-in.** The RDR's Phase 3 kept the check off until step 4 (the live measurement) and Sam's approval. The first live canary added 118 s to the G06 run (four serial calls, one cut at 60 s), and the latency fix, concurrent assessment calls in packs of at most 20 anchors, grew into its own bead (nexus-jypmj.17). Sam decided to ship the default together with that fix. The measurement followed: on 29 held-out requests wall latency was +16.8 s at the median and +33.9 s at the 90th percentile, with 0 errors and 0 withheld answers. *Drift class: scope underestimation.*

### Existing infrastructure reused

- The run record and the composite telemetry write (RDR-203) carried the summary; the one new column, `out_of_step_cost_usd`, is a nullable addition.
- The plan matcher's existing correctness filters, `choose_within_band` and the inline planner's few-shot renderer took the eligibility gate.

### Added beyond the plan

- A coverage state `unresolved` for an ambiguous or failed named-source lookup (two projects carry the same RDR number); before it, the check read such a source as `missing` and skipped generation at zero cost.
- The `node_walk` engine response, the `secondary_omitted` disclosure for a capped walk, and the active-project tier of source resolution.
- The measurement harness: drivers, a drafted-label oracle, a 15-pair spot check, and the golden, held-out, compound, near-miss and negative-control request sets.

### Planned but not implemented

- Calibration of `max_documents` and `max_chunks`. The RDR said both would be calibrated; they are the constants 50 and 300, and the one measurement taken (all 38 one-hop documents of G06 lost to two-hop ones under role-before-distance ranking) argues against 50.
- A labelled oracle for the five largest held-out requests, and a measured false-withhold rate.

---

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 2 | Directional lookup row; negative controls clean of the tenant | Yes — spike (run the near-miss control before enabling the rule); Yes — source search (search the tenant, not the repo) |
| **Missing failure mode** | 2 | Relay loss under the engine filter; an unreadable facet that blocks the withhold | Yes — spike (a relay node in the fixture; a case with more anchors than a call carries) |
| **Under-specified architecture** | 1 | An owner is not a collection set | Yes — source search (the predicate's type was readable) |
| **Scope underestimation** | 1 | The default-on flip and its latency fix | No — a decision made with new evidence |

### Pattern references

No `SYNTHESIS.md` exists in `docs/rdr/post-mortem/`; no pattern is cited. Divergences 2 and 4 repeat one pattern: a control or pin that tested something near the thing claimed (the repository, the in-sample set) and not the thing itself.

---

## RDR quality assessment

### What the RDR got right

- It separated the three failure surfaces and refused to fix only the matcher; the live runs show each gate doing its own work (eligibility excludes plan 473, scope keeps code out of the vector stream, the check withholds N01 to N04).
- It put the routing constraint ahead of similarity and cost, and it kept a measured path to remove the model-assisted repair if its yield did not pay.
- Its Key Discoveries recorded dead ends and corrections as findings (the stale `section_title` assumption, the A5 corrections), which is why the later reversals could be dated and attributed.

### What the RDR missed

- A walk through one real graph with the filter on. Every graph test used a fixture whose in-scope nodes sat on direct paths.
- A rule for the facet the check cannot read, as a separate case from the facet the check read and found unsupported.
- Which of its own controls could have caught the directional row. The near-miss set was written two days before it was run.

### What the RDR over-specified

- **Calibrated caps:** two numbers promised from measurement that were never measured.
- **Performance targets unvalidated:** the unit-test timing model (50 model-seconds) was cited as expected latency before any live run; the live figure was 54.3 s, close, but it was a model of assumed call times.

---

## What stays open

- **The primary source is capped at 24 chunks on a single-source synthesis** (nexus-jf9wg). Every near-miss run read 24 of 41 to 135 chunks of the named record, and four of six answers said a requested section was not in their input. This is the largest open quality gap.
- **The synthesis row admits citation-traversal (467) for single-source, section-spanning synthesis** (nexus-4xmpv), which the set's labels list as unacceptable: 2 of 6 near-miss requests.
- **The coverage check reported a facet `covered` that the answer's own prose called missing** (nexus-dfrfp): S03 f3 ("Part 3 is not supported"), S04 f1 ("Gap 2 is not among the supplied passages"), S06 f3 (the passages do not record whether P1 to P4 are done). Three of 18 facets, three of six near-miss runs, all behind the 24-chunk primary cap. The assessor's permissive side is weaker than the negative controls alone suggest.
- **A request whose extraction call fails keeps the gate enforcing on first-pass kinds** (nexus-x3eoc), the regime measured as failing. It rests on the observed 0 of 112 extraction failures; nothing counts or alarms on a run of failures beyond the once-per-streak log warning. Whether to degrade such a request to shadow is Sam's decision.
- **Covered facets with most of their anchors unread are never repaired** (nexus-z6yvk). H12, H13 and H28 read 116 to 156 of 261 to 325 anchors; the rest is the tail of the graph stream.
- **Unknown-kind grown plans are still admitted for lookups.** The wrong-shape exposure the lookup-only rule removed for synthesis plans remains for them, and no measured run selected one (plan growth was off).
- **Caps and repair.** The `max_documents` and `max_chunks` calibration; clause-facet repair yield (0 of 5 tries); the repair keep-or-drop call, which was recommended "keep" and is Sam's.
- **Unmeasured**, as the RDR's Performance Expectations lists: the full golden set on the concurrent design, small budgets (nexus-jypmj.22), an oracle for the large requests, and the `claude -p` backend under load.

## What the next person should take from this

**Run the control you wrote before you ship the rule it controls.** The near-miss synthesis set existed on 2026-10-28. The directional rule and the default-on check were both live before it ran, and it found the 24-chunk cap, a 467 admission and a ledger that disagreed with its prose.

**Test a filter on the real graph, not the fixture.** The fixture put every in-scope node on a direct path, and the live G06 walk put 25 of 30 behind an out-of-scope relay.

**A check must be able to fail on a case you built to make it fail.** N04 had more anchors than a call carries, and the first version of the rule made withholding impossible for it. Build the case that should be withheld, with the same size as production, before trusting a green run.

**Prove a negative control is absent from the thing it runs against.** Searching the repository for a marker says nothing about the tenant.

**Measure develop code from an installed copy, not a checkout.** `uv run` inside the repository imports the code from the checkout, and the production-write guard then refuses the plan matcher's write on every plan hit and drops the run row on a miss. Two attempts on 2026-10-30 returned `[non-answer: error]` or dropped their rows, and one cost 0.5263 USD for no data. Install the tree non-editable into a virtualenv outside the repository and check `is_dev_checkout_process()` prints `False`. The measurement driver now refuses to start from a checkout and prints this recipe (`scripts/bench/rdr232_phase3_measure.py`); the guard itself was not changed.

## Critique record

The close audit (T2 `nexus/review-rdr232-premise-closure-critique-2026-10-30`) found 0 Critical and 5 Significant findings, all documentary or tracking: stale Status lines and unticked boxes, section 3 not amended for the shipped rule, Phase 3 step 4 results living only in a bead, open measurements with no bead, and the lookup row still reading as gated. The close sweep of 2026-10-30 (nexus-jypmj) rewrote those sites; the RDR's status was left to the lifecycle step.
