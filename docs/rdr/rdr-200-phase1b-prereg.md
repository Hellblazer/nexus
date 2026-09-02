---
title: "RDR-200 Phase 1b Pre-Registration: re-gate on the repaired retrieval substrate"
parent_rdr: RDR-200
parent_bead: nexus-4e75w
supersedes_result: docs/rdr/rdr-200-phase1-gate-result.md
inherits_protocol: docs/rdr/rdr-200-phase1-prereg.md
question_set: docs/rdr/rdr-200-phase1-questions.md
created: 2026-09-01
status: frozen-before-arms
---

# RDR-200 Phase 1b Pre-Registration

A FRESH pre-registration, written before any Phase 1b arm runs, as the
Phase 1 result required ("a different experiment ... would require a
fresh pre-registration written before any new arm runs"). It inherits
every protocol value from `rdr-200-phase1-prereg.md` verbatim except
where this file says otherwise, and it pins the retrieval substrate the
arms run on, which is the one thing that changed.

## 1. Why a second gate

Phase 1 FAILED its rule 0/24 against caller-only, in both strata, and
the judges' reasons converged on corpus reach rather than reduction
quality: the plan-based arms retrieved through two defects the gate
itself surfaced (prose starvation on the cross-model merge, and
implementation source buried under test files by a relevance-blind
file-size penalty). Sam's decision (T2
`nexus/decision-retrieval-fix-zero-cost-first-2026-09-01` [23974]): fix
the retrieval defects, then re-gate with retrieval fully fixed, because
composed retrieval is the signature feature and a verdict on a
half-fixed substrate is uninterpretable.

## 2. Substrate pinned for Phase 1b

- **Client:** develop at `76b0d1fc1` (a9b229e7f plus two docs/lint commits); the
  serving generation was reinstalled from that tree and the MCP servers
  reconnected (`/mcp`) before any arm. The two repairs the arms depend
  on, both landed and reviewed:
  - `b390840ea` nexus-tox2m: cross-model distance calibration keyed on
    the resolved embedding model, one pooled window over calibrated
    distances, hard no-op when a result set spans one model.
  - `a9b229e7f` nexus-0bmhd (closes nexus-vlzz0): RDR-006's file-size
    penalty removed from scoring; a render-layer diversity cap (at most
    2 chunks per file lead a page) applies to text renders only.
    `structured=True`, which the plan runner injects on every retrieval
    step, sees the uncapped ranked order.
- **Engine:** api.conexus-nexus.com `release_version` 0.1.93,
  `embedding_mode` voyage, unchanged since Phase 1.
- **Tiering:** `src/nexus/operators/model_tiers.py` unchanged since the
  Phase 1 transcription (no commits to the file after `936bd94d6`);
  `STRONG_DEFAULT_ALIAS = "opus"`, `FLIPPED_OPERATORS` = {extract,
  filter, groupby, rank, check, verify}. Production default tiering,
  `NX_OPERATOR_MODEL_TIERING` unset, at both dispatch sites (Phase 1
  prereg §3 pin).
- **Continuation:** live, opt-in, `MAX_CONTINUATION_PROMPT_CHARS`
  unchanged from Phase 1.

## 3. Declared caveats (known, accepted, not to be discovered later)

1. **Catalog-routed retrieval tools are uncalibrated.**
   `search_metadata_scoped`, `search_graph_hop`, `search_topic_scoped`,
   `search_aspect_scoped` bypass `apply_ranking_boosts` and rank on raw
   cross-model distance. Measured on the Phase 1 set: none of the 24
   questions' plans route through them unless an explicit `scope` is
   passed, and no arm passes one. If a Phase 1b plan does route through
   one, the gate report must say so per question.
2. **Legacy 2-segment collection names defeat calibration**
   (nexus-mc1l1, open). Every collection in the default fan-out on this
   install carries the conformant 4-segment name, so the caveat is
   inert here; recorded because a different install would not be.
3. **The crowding score measures the presentation channel.** The
   crowding procedure uses the flat `search` tool's text render, which
   now carries the diversity cap; the plan runner reads the structured
   channel, which does not. Phase 1's penalty distorted both channels
   identically, so Phase 1b's crowding is, if anything, a slightly
   cleaner view than what the plan-based arms see, never a worse one.
4. **The frozen question file is indexed and self-retrieves.** Sam's
   decision for Phase 1b (2026-09-01): reuse the frozen 24 for direct
   comparability with the Phase 1 verdict rather than assemble a new
   held-out set. Exposure was symmetric across arms in Phase 1 and is
   again here. The crowding re-score records, per question, whether
   the file surfaced in the flat top-10.

## 4. Question set and crowding

Same 24 question texts as `rdr-200-phase1-questions.md`, verbatim, same
ids. **Crowding is re-scored** on the repaired substrate by the
identical procedure (one flat `search` call, default fan-out, default
threshold, `limit=10`; one blind `claude -p --model opus` labeling per
question with the identical instruction text; crowded = irrelevant
fraction >= 0.5). Strata are **reassigned from the new scores** and
frozen here before any arm runs; the Phase 1 strata are recorded
alongside for comparison, never used for the verdict.

Stratum minimum stays 8. If the repaired substrate leaves fewer than 8
crowded questions, the crowded stratum is reported as below minimum and
the stratified prediction is INCONCLUSIVE for Phase 1b under the Phase 1
no-signal protocol; the pooled comparison still runs and is reported on
its own terms. The set is not extended for Phase 1b, so that the
comparison to Phase 1 stays question-for-question.

### Re-scored strata (filled at freeze, before any arm)

Re-scored 2026-09-01 on the Phase 1b substrate; judge `claude-opus-5`; labeling spend $9.54 over 24 dispatches. **Crowded 10 / clean 14**; 6 question(s) changed stratum versus Phase 1 (12/12). Self-retrieval of the frozen file in the flat top-10: 0/24.

| id | Phase 1 score / stratum | Phase 1b score / stratum | moved |
|---|---|---|---|
| q01 | 1.0 / crowded | 0.3 / **clean** | yes |
| q02 | 1.0 / crowded | 1.0 / **crowded** |  |
| q03 | 1.0 / crowded | 0.9 / **crowded** |  |
| q04 | 1.0 / crowded | 1.0 / **crowded** |  |
| q05 | 0.6 / crowded | 0.6 / **crowded** |  |
| q06 | 0.7 / crowded | 0.3 / **clean** | yes |
| q07 | 0.5 / crowded | 0.0 / **clean** | yes |
| q08 | 0.5 / crowded | 0.0 / **clean** | yes |
| q09 | 1.0 / crowded | 0.6 / **crowded** |  |
| q10 | 1.0 / crowded | 1.0 / **crowded** |  |
| q11 | 1.0 / crowded | 0.6 / **crowded** |  |
| q12 | 1.0 / crowded | 1.0 / **crowded** |  |
| q13 | 0.1 / clean | 0.8 / **crowded** | yes |
| q14 | 0.0 / clean | 0.0 / **clean** |  |
| q15 | 0.4 / clean | 0.6 / **crowded** | yes |
| q16 | 0.4 / clean | 0.3 / **clean** |  |
| q17 | 0.0 / clean | 0.0 / **clean** |  |
| q18 | 0.1 / clean | 0.0 / **clean** |  |
| q19 | 0.3 / clean | 0.1 / **clean** |  |
| q20 | 0.1 / clean | 0.0 / **clean** |  |
| q21 | 0.1 / clean | 0.0 / **clean** |  |
| q22 | 0.3 / clean | 0.4 / **clean** |  |
| q23 | 0.2 / clean | 0.2 / **clean** |  |
| q24 | 0.4 / clean | 0.2 / **clean** |  |

Both strata clear the minimum of 8. Frozen at this commit; no arm has run.

## 5. Arms, judging, pass/fail

Unchanged from the Phase 1 prereg §§3-5: three arms (continuation,
headless with `continuation=false`, caller-only with no `nx_answer`),
arm model `claude-fable-5` for the two in-context arms, judge
`claude-opus-5`, blinded pairs in both position orders, rubric verbatim
from Phase 1, simple majority with ties against continuation, pass =
continuation >= headless AND > caller-only, entropy prediction = margin
over caller-only largest in the crowded stratum, INCONCLUSIVE is not a
pass. Arm briefs are the Phase 1 briefs with only the paths changed.

Added for Phase 1b, reported but not traded against the verdict:
per-question **retrieval reach** for the plan-based arms, taken from the
`trace=true` step output: whether the primary source the caller-only
arm cited was present in the plan's evidence. Phase 1's mechanism
finding was corpus reach; Phase 1b must show whether that moved,
independently of who won.

## 6. Telemetry side

Unchanged from Phase 1 prereg §6.

## Revision History

- 2026-09-01 (before any arm ran) — **Arm session-model id recorded
  exactly**: the orchestrating session for Phase 1b runs on
  `claude-fable-5-1`, so the continuation-arm reducer and the caller-only
  reasoner (both dispatched in-process from this session and inheriting
  its model) run on `claude-fable-5-1`. Phase 1 recorded its arm model
  as `claude-fable-5`; whether that was the same model id written
  loosely or a genuinely earlier session model cannot be re-verified
  now, so Phase 1b records the exact id and notes the possible drift.
  The same-model constraint (continuation vs caller-only) holds within
  Phase 1b. Judge stays `claude-opus-5`. Arms save the raw `nx_answer`
  return alongside the answer (`qNN.raw.json`) so the §5 retrieval-reach
  measurement is taken from the recorded trace, not re-run.
- 2026-09-01 (after the first caller-only pass, before any caller-only
  verdict was tallied) — **Caller-only arm re-run for q14-q24 under an
  explicit MCP-only tool restriction.** The first pass's agent for
  q13-q24 disclosed that q14-q24 were answered from direct repository
  reads (Bash grep/sed over source, RDRs and docs) and from injected
  session memory, not from the indexed corpus through the retrieval
  tools. That is not the arm the protocol defines (flat search over the
  same corpus the plan-based arms retrieve from), and it hands the arm a
  filesystem the other two arms do not have. The q01-q12 agent stayed
  on MCP tools only; q13 used the paper corpus only. The eleven affected
  answers are preserved as `calleronly-unconstrained/` in the gate
  artifacts and are NOT judged; q14-q24 are re-run with Bash/Read/Grep,
  T1/T2 and injected memory forbidden as evidence, and only those
  re-run answers enter the caller-only pairs. The re-run brief is the
  original brief plus the restriction; nothing else changed. Judges
  already dispatched on the first-pass caller-only pairs for q14-q24
  have those verdicts discarded unread. Whether Phase 1's caller-only
  arm stayed on MCP tools cannot be re-verified (its subagent
  transcripts are gone); Phase 1b records the restriction explicitly so
  the comparison to Phase 1 carries that caveat.
