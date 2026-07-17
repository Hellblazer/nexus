# Post-Mortem: RDR-185 — Single-Ladder Convergent Upgrade

**Closed:** 2026-07-17 (implemented) · **Span:** 2026-07-16 → 2026-07-17 ·
**Epic:** nexus-n7u38 (P0–P5, all closed)

## What shipped

One ordered migration ladder across every axis (T2 schema, T3 substrate, chunk
identity via wire re-id, embedder era), rungs detect → converge → verify,
idempotent, immutable-source. `nx upgrade` walks it; `nx doctor` reports it;
every other upgrade verb demoted. The era-hop container leg proves the flagship
promise live: a conexus 6.0.0 install with pre-RDR-108 legacy ids reaches
current via `nx upgrade` alone, unattended. The Decision stayed one paragraph.

P5 (post-gate) closed the last inverted criterion — the Gap-5 census asked the
immutable SOURCE and so reported era debt forever on converged installs — and,
via its review arc, found and fixed three latent rung defects (the billed-Voyage
consent gate that had never fired in production; a two-sources-one-target silent
merge; the credential-gated collection that vanished from every surface) plus a
keyed-mode mislabel mis-plan. Constraints were amended twice en route: the
billed re-embed is the third genuine decision, and a permitted prompt must have
an unattended channel (`nx upgrade --yes` / `NX_ASSUME_YES`).

## Success-criteria accounting (stated exactly)

- **Gap-5 (era debt visible the day it exists):** proven live; the census asks
  the target and the era-hop asserts the row.
- **SC-1 + SC-2 (ancient install converges unattended; zero re-embedding for
  pure id conformance):** proven over the entire supported origin set —
  compositionally: consent end-to-end (subprocess gate) ∘ free-shape
  convergence (in-process, real uninjected gate) ∘ bge convergence (era-hop).
- **"Applies across local, service, managed-cloud":** local + service proven.
  Managed-cloud MIGRATION resolved by **scope reduction, not verification** —
  the population decree (2026-07-17) retired every voyage-keyed migration shape
  (sole install migrated 2026-06; managed onboarding is greenfield-only;
  thirteen beads closed as mooted). Recorded as decree-scoped, deliberately.
- **Gap-4 (exactly two answer mechanisms):** held, and made evaluated rather
  than architectural (`test_rung_convergence_is_re_derived_live_never_cached`).

## What it cost, honestly

Five defects shipped-and-fixed in P4, none visible to a green unit suite. P5
then ran **nine review rounds**; the first **six consecutive fix rounds each
introduced a new defect**, three of them Criticals in the fixes themselves
(a collision guard that bricked `nx upgrade` on a healthy install; a consent
gate that turned silent billing into an unattended hard-fail with an empty
message; a predicate that made the RDR's own flagship install defer forever at
exit 0 while reporting success). Product code was clean from round five on; the
remaining rounds found verification defects — **nine vacuous test pins plus one
latent**, each passing for a reason unrelated to the property it named.

## The two transferable lessons

**1. The proxy pattern.** Every product defect in this arc was one shape: a
proxy standing in for ground truth, correct in the tested branch and wrong in
the untested one. The source proxying for the target (the census). A stored
flag proxying for a derived fact (the billed consent). `needs_reembed` proxying
for "will bill". `isatty` proxying for "is anyone there". Key-presence proxying
for "what does the service wire". A collection's name proxying for its content.
"The service was dialled" proxying for "the gate was passed". The fix altitude
is always the same: ask the thing that knows, at the moment it knows.

**2. Pins written from prose.** All ten test-pin failures shared one root
cause, named by the substantive-critic and confirmed across every instance:
the pins were written from the commit message (later: from the critique)
rather than from the code. Species catalogued in the project memory
(`feedback_falsify_by_deleting_the_code`): fixture short-circuits in control
flow (4), in time (1), in the shared fixture (1), on the output surface (3 —
merged-stream greps satisfied by structlog lines carrying the same string),
plus one latent rung-identity proxy. The counters that actually worked: write
the mutation before the test; select stream + prefix + a marker only the
claimed path can produce; run the real thing (six lines of live planner
reproduced a Critical two full review rounds had missed); clear `__pycache__`
before subprocess mutation runs.

## The stopping rule (adopted)

Review recursion does not terminate on its own — round nine was reviewing the
verification of the verification. The rule that ended it, from the critic and
applied: **when product code is unchanged across a round AND every named
property has a red-on-regression mutation, the gate is closed; remaining
findings are maintenance, applied without another adversarial round.**

## Survivors

- **nexus-ixl85** — detect()'s classification sweep on every `nx doctor`,
  forever, on converged installs. Retires naturally at RDR-155 P4b (the
  footprint dies with the migration module). The obvious interim fix
  contradicts the Gap-4 live-derivation invariant; do not fix casually.
- **nexus-d8mts, nexus-z5j0t** — P2 residuals (T2-rung critique findings;
  CHASH_BEARING_TABLES extension), tracked, not load-bearing for any criterion.
- **RDR-155 P4b** remains the standing DO-NOT-START boundary; the demoted
  verbs, the Chroma read client, and the census's reason to exist all end there.

## Records

T2 `nexus_rdr/185-phase{0..4}-gate`, `185-p5-DONE` (the full P5 ledger),
`185-p5-51djh-managed-gate-finding` (where the managed health-gate guarantee
actually lives, source-read), plan `nexus/plan-rdr-185.md`. Twelve P5 commits,
`49533397..a881cadc`.
