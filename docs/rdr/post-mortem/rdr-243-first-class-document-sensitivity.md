# RDR-243 post-mortem: first-class document sensitivity

## What the RDR set out to do

Keeping a document away from language models and outside services was operator
knowledge, not product surface. A document stayed out of aspect extraction only
because of the collection it sat in. No catalog row or search hit said it was
sensitive, and the surfaces that send content to a model or a third party
(aspects, taxonomy labelling, `nx_answer` operator steps, bibliographic
enrichment, the reranker) did not know a flag could exist. The RDR put one
`sensitive` flag on the catalog document. A registry lists every surface that
sends content out, with the behaviour each must follow for a flagged document
(refuse, skip or withhold) and the phase that enforces it. Generated help and
privacy text name only the rows the shipped release enforces.

## Implementation status

Implemented, in three releases. Phase 1 (the flag and the input side) shipped in
conexus 7.96.0 with engine-service-v0.1.180. Phase 2 (read-side filtering for
`nx_answer` and tool-access children) shipped in 7.97.0. Phase 3 (output
withholding in MCP and the CLI, the rerank exclusion, the DEVONthink import and
the external-embedder doctor row) shipped in 7.98.0 with engine-service-v0.1.182.
Epic nexus-rfvyy is closed, and every phase bead is closed. Sam closed the RDR on
2026-11-08 ("Close, carry 3 forward", ask halcyon/rdr243-close). Three items
moved to nexus-rfvyy.9. No close critique ran: Phase 3 had already passed
code-review-expert, substantive-critic and test validation (nexus-rfvyy.8.10 to
.8.12). That skip was the closing session's judgment
(T2 `nexus_rdr/243-close-override-2026-11-08`).

## What diverged

**The phase plan changed twice after acceptance.** The gate passed with phases 1
and 2 shipping together. Sam then split them, and Phase 1 shipped alone in
7.96.0. Because each registry row already carried its enforcing phase, the
generated text could follow the split without a rewrite. That per-row phase was
added in a fix round before acceptance, and it paid for itself here.

*Drift: scope underestimation* (the ship-together plan assumed the phases would
finish together).

**Sam made most of the decisions during implementation, not at the gate.** The
gate settled Q1 to Q8. Reviews later needed rulings on duplicate merges, tombstone
inheritance, the aspect drain, Voyage-profile refusals, `nx store import`, and the
child file-read limit. Each was recorded as a Revision History row in the same
push. The lesson from RDR-239 held, but this many rulings means the design reached
the gate with open edges.

*Drift: under-specified architecture.*

**The engine does not filter every route.** The Phase 2 engine critique (M5)
found that `/get-all-metadata`, `/store-list`, `/get-embeddings` and `/reapable`
still serve titles and embeddings of flagged chunks under `sensitivity=exclude`.
Phase 3 withholds on the client, so no shipped surface leaks through these
routes, but the engine-side claim is narrower than the design read. Carried in
nexus-rfvyy.9.

*Drift: missing failure mode.*

**The output posture covers fewer verbs than a reader would assume.** Q2 decided
the posture for `nx search`, `nx store get/list` and the `nx catalog`
show/list/search/links verbs. The docs critique found that other CLI verbs print
flagged titles and paths under Claude Code, and that exact-title lookups act as
existence oracles. `docs/sensitivity.md` records them as not enforced. Extending
the posture needs a design decision. Carried in nexus-rfvyy.9.

*Drift: missing cross-cutting concern* (output went through more surfaces than
the census listed).

**Tests that reached storage without a tenant turned develop red.** The Phase 3
push reddened five of the six CI shards (1, 2, 4, 5 and 6): new catalog reads at the withhold points reached
storage from tests that had never needed a catalog. The fixes were seams with
stand-ins (the `_default_reader` for the child file-read deny, a catalog stub for
the GC wire suite), not changes to the gates.

*Drift: missing cross-cutting concern.*

**A Windows path gap surfaced in passing.** `search_engine._local_source_path`
accepts only `file:///` URIs, so on Windows registered documents get no local
source path. It is not a leak, because the child file-read deny parses every
Windows spelling and fails closed. Carried in nexus-rfvyy.9.

*Drift: framework API detail.*

## What to carry forward

- When a feature adds a check at an output or read point, run the suite against
  the census of tests that reach that point before the push. A new catalog read
  in a shared path turns red every test that never set a tenant.
- A census of surfaces that send content out should list output verbs as well as
  input verbs. The missing rows here were all output.
- Closing an RDR as implemented needs a post-mortem or a frontmatter waiver.
  `tests/test_rdr_close_tripwire.py` enforces it, and this close first shipped
  without one and turned develop red at 962a93b3c.
- Open at close: nexus-rfvyy.9 (engine routes, CLI output posture, Windows
  source path).
