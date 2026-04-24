# Post-Mortem: RDR-088 AgenticScholar Operator-Set Completion

## RDR Summary

RDR-088 closed three paper-operator gaps from the 2026-04-17
AgenticScholar retrospective: `operator_filter` (§D.4), `operator_check`
(§D.2), and `operator_verify` (§D.2). A fourth gap — `plan_match`
discrimination in the 0.40-0.65 ambiguous confidence band — was
conditional on a prerequisite spike measuring whether an LLM rerank
pass added signal **after** RDR-092's hybrid `match_text` + per-call
`min_confidence` mitigations. The spike was designed around a
pre-agreed dual threshold so the Phase 3 landing decision was
unambiguous before any production code shipped.

## Implementation Status

**Partial — Phases 1+2 implemented, Phase 3 closed via spike verdict.**

Paper operator coverage after landing: 11/13 = 84.6%. `GroupBy` and
`Aggregate` (§D.4) remain deferred per the RDR's explicit scope
subsection; no follow-up bead filed until a concrete compositional
query demands them.

### Phase 1 — `operator_filter`

Landed on branch `feature/nexus-ac40.1-operator-filter`:

- `src/nexus/mcp/core.py:1618` — `operator_filter(items, criterion, timeout)`
  returning `{items: list[dict], rationale: list[{id, reason}]}` via
  `claude_dispatch`.
- `src/nexus/plans/runner.py` — extended `_OPERATOR_TOOL_MAP`,
  `_INPUTS_TARGET`, the ids-branch auto-hydration, and the
  list-to-JSON coercion. The audit carry-over from `nx_plan_audit`
  pre-committed to these exact edits so the hydration-path wire-up
  couldn't reproduce the 4.10.1 `nexus-yis0` class of `TypeError`.
- `src/nexus/plans/bundle.py` — `BUNDLEABLE_OPERATORS` + the
  `_describe_step` + `_terminal_schema` filter branches.
- 17 offline tests (5 operator-dispatch + 5 hydration + 1 bundle +
  2 integration + existing parametrised coverage).
- Phase 1 gate: PASSED, one PEP-8 nit and one strict-`<` → `<=` fix
  landed in-place.

### Phase 2 — `operator_check` + `operator_verify`

- `src/nexus/mcp/core.py:1681` — `operator_check(items, check_instruction, timeout)`
  returning `{ok: bool, evidence: list[{item_id, quote, role}]}` with
  role enum ∈ `{supports, contradicts, neutral}`.
- `src/nexus/mcp/core.py:1735` — `operator_verify(claim, evidence, timeout)`
  returning `{verified: bool, reason: str, citations: list[str]}`.
- Module-level `_CHECK_EVIDENCE_ITEM_SCHEMA` in `core.py` referenced
  from `bundle.py`'s `_terminal_schema` check branch. The first Phase 2
  pass inlined the schema in both files; the Phase 2 gate code-review
  surfaced it as the only blocking finding; fixed in a follow-up
  commit before close.
- Runner + bundle integration followed the filter precedent. Verify
  was intentionally excluded from `_INPUTS_TARGET` so stray `inputs`
  args surface as authoring bugs rather than silent renames.
- 18 offline tests (9 operator-dispatch + 7 hydration + 2 bundle).
- Phase 2 gate: PASSED.

### Phase 2 integration (live-I/O)

Two `@pytest.mark.integration` tests on `knowledge__delos` verified
end-to-end wiring with real `claude -p` dispatch. Both passed on the
2026-04-24 run:

- `TestTraverseThenCheck` — search → hydrated `operator_check`, ~65s.
- `TestPhase2MVVPipeline` — `search → filter → check`, filter+check
  bundled into one `claude_dispatch`, ~40s.

Corpus-drift surfaces as `pytest.skip` rather than silent fail. Fixture
provenance persisted to `nx memory nexus_rdr/088-research-4` (id=961).

### Phase 3 — `plan_match` LLM rerank

**Did not land.** The prerequisite spike (`nexus-ac40.8`, Spike B)
measured:

| Metric    | Config A (rerank-off) | Config B (rerank-on) | Delta   | Threshold |
|-----------|-----------------------|----------------------|---------|-----------|
| Precision | 0.5455                | 0.6667               | +0.1212 | ≥ 0.05 ✓  |
| Recall    | 0.6000                | 0.4000               | -0.20   | > -0.15 ✗ |

Dual threshold failed by 0.05 absolute on recall. The 0.90 LLM
confidence gate correctly suppressed 3 FPs (qb-18/19/20) but wrongly
suppressed 2 TPs (qb-03/10) — net composite quality slightly worse
than the baseline. Gap 4 closes as "already addressed by RDR-092".
Spike B's dual-threshold wording was load-bearing — it caught the
precision-preferred trade the RDR's Consequences section had promoted
to a first-class concern during the gate.

Conditional impl beads (`nexus-ac40.9/.10/.11`) force-closed with the
spike verdict. Prototype rerank at
`scripts/spikes/spike_b_rerank_prototype.py` retained locally for
audit but not committed per the RDR's "throwaway" framing.

Spike A (`nexus-ac40.7`, operator_check verdict stability) ran
orthogonally at the same session and returned 95% fully-stable rate
across 20 fixtures × 5 runs with 0% schema-validation errors —
increases confidence in `operator_check` as shipped in Phase 2.
Report at `nx memory nexus_rdr/088-spike-a-check-stability` (id=964).

## Divergences from Technical Design

- **Bundle integration scope**: the RDR specified plan_run isolated
  dispatch as the minimum. Bundle integration (`BUNDLEABLE_OPERATORS`
  + per-verb `_describe_step` + `_terminal_schema`) landed inline
  with the isolated-dispatch work because consecutive operator steps
  without bundle support would split bundles at filter/check/verify
  boundaries, creating silent performance regressions. Tight scope
  match; not documented as a scope expansion because it completes the
  operator integration story.
- **No runtime subset validator for `operator_filter`**: the "output
  items must be subset of input" invariant is prompt-enforced, not
  code-validated. Phase 1 code review flagged this as non-blocking;
  can ship as a hardening follow-up if LLM hallucination is observed
  in practice. No incidents through close.

## Outstanding Follow-ups

- `nexus-4o2z` — hoist `_INPUTS_TARGET` dict to module scope. Filed
  during Phase 1 gate; closed in-line before RDR close (commit
  `4e69804`).
- `nexus-b8pe` P3 — `bd` missing `dep-remove` subcommand (surfaced by
  the audit carry-over's need to clear a stale blocker edge on
  `nexus-ac40.9`). Not nexus code; tracked for bd maintainer.
- `nexus-e59o` P3 — `nx memory get --title` prefix/substring match
  (surfaced by Phase 2 gate observations). Low priority; separate PR.

## Persistent Artifacts

All in `nexus_rdr` memory project (permanent TTL):

- `088-research-1` (id=?) — RDR-092 baseline for Gap 4 spike
- `088-research-2` — `claude_dispatch` schema-conformance evidence
- `088-research-3` — authoritative operator inventory (5 + 4 = 9
  composable pre-RDR-088; 8 + 4 = 12 post)
- `088-research-4` (961) — integration-test fixture provenance
- `088-phase1-gate` (960) — Phase 1 gate report
- `088-phase2-gate` (962) — Phase 2 gate report
- `088-spike-b-llm-rerank` (963) — Phase 3 FAIL verdict
- `088-spike-a-check-stability` (964) — operator_check stability PASS

T3 (`knowledge__nexus`) has two decision artefacts tagged
`decision,spike,rdr-088`: one per spike.

## Lessons

1. **Dual thresholds earn their keep**. Spike B produced precision
   +0.1212 which would have cleared a single-threshold gate. The
   recall-delta threshold (-0.15) was the load-bearing part. The
   precedent: whenever a measurement gate informs a conditional
   landing, a single-sided threshold is an anti-pattern.

2. **Audit carry-overs reduce wire-up risk**. The `nx_plan_audit`
   carry-over on `nexus-ac40.1` and `.4` pre-committed to the exact
   hydration-path edits. The 4.10.1 `nexus-yis0` class of bug did
   not recur. When the review-gate work pre-commits to specific
   file:line edits, downstream impl becomes mechanical.

3. **Bundle integration is not optional for operator families.**
   Adding an operator to `_OPERATOR_TOOL_MAP` without extending
   `BUNDLEABLE_OPERATORS` silently splits bundles at the new
   operator's boundary. The Phase 1 test suite did not catch this
   because no existing test constructs a bundle across the new
   operator. Noted for future operator additions.

4. **"Throwaway" needs a storage policy.** The Phase 3 prototype
   code (`scripts/spikes/spike_b_rerank_prototype.py`) and the
   measurement artefacts are not in git per the RDR's framing, but
   they exist locally on the author's machine. Reproducibility
   depends on the T2/T3 summaries, not the raw scripts. Future
   spike-heavy RDRs should either commit the scripts explicitly or
   package the summaries richly enough to stand on their own.
