# RDR-134 post-mortem: taxonomy-aware recall in nx_answer

## What the RDR set out to do

`nx_answer`'s prose steps search about 123 collections at once, and generic
documents (changelogs, READMEs) crowd out the specific ones: across the
production set the RDR questions scored 0.335 NDCG@3, against 0.591 for the
same questions searched in one collection (`134-research-13`). The collections
already carry topics. The RDR added a route that searches each collection only
inside the topics nearest the query (Phase 1, engine and client), wired it into
`nx_answer` behind a setting (Phase 2), and made the setting's default depend
on two pre-declared checks: the Minimum Viable Validation (Step 5a-ii, the
engine route in plain search) and an `nx_answer` A/B (Step 9).

## Implementation status

Abandoned (Sam, 2026-11-19). Every step was built and both checks ran; both
failed. The validation reached 0.385 NDCG@3 against a 0.44 bar, two questions
lost more than 0.1, and latency was 1.66 times the unrouted request (a soft
figure: one run, fixed order, and a different search path). The A/B lowered
generic-answer questions from 0.654 to 0.496 (`-17`, `-18`). The setting
`search.topic_route_nx_answer` stays off; the engine route (engine 0.1.197) and
the client option stay in the code, dormant. Step 6 (a second embedding for
the centroid ranking) was not run, by Sam's decision. The fan-out recall
problem is still open, with no owner.

## What diverged

**The client-side spike did not predict the engine route.** The spike
(`-13`) routed in the client with a per-collection k of 20 and gained +0.135;
the engine route at production's k of 40 gained +0.05 over the same baseline.
The cheaper candidate causes the validation's bead listed (k, thresholds,
rerank) were never isolated, so the gap is unexplained.

*Drift: unvalidated assumption.*

**Routing could not reach most plans.** As first built, only steps of plans
declaring `taxonomy_domain: prose` were routed, and only three builtin
templates do. The first A/B probe routed nothing. Sam chose to route any step
whose scope names no other domain (option b), which changed Step 7's design
after the RDR was accepted.

*Drift: under-specified architecture.*

**The A/B measured a search bug first.** The first partial A/B scored 0 in
both arms on every question of the main plan: the search pool was cut by raw
distance before calibration, so code chunks displaced the docs (wren's fix,
5b366f0d5). The A/B's pre-declared primary ordering, raw distance, had the
same blindness, and Sam changed it to calibrated distance after seeing those
rows. Both are disclosed in the RDR.

*Drift: missing cross-cutting concern.*

**Phase 2 ran before the Phase 1 check it depended on.** The plan said Phase 2
starts only on a passed validation. The A/B was scheduled as soon as the
engine was deployed, and the validation ran afterwards. It failed on its own,
so the order did not change the outcome.

*Drift: deferred critical constraint.*

**A spend guard found a reporting gap.** The A/B stopped on an unknown cost.
The cause was `nx_answer`'s single-query fast path, which recorded no step and
so reported its cost as unknown. Fixed on the way (77c1299b5).

*Drift: missing failure mode.*

## What to keep

The pre-declared bars and stop branches worked: each check ended in a decision
the numbers forced, not one argued afterwards. Measure the shipped path, not a
client-side stand-in, before building the wiring that depends on it.
