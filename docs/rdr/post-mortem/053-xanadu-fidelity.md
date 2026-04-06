---
rdr: "053"
title: "Xanadu Fidelity — Tumbler Arithmetic and Content-Addressed Spans"
status: closed
closed_date: 2026-04-06
reason: implemented
---

# RDR-053 Post-Mortem

## Outcome

Fully implemented. Both phases (Tumbler Arithmetic + Content-Addressed Spans) shipped in 14 commits across 10 files, with 241 tests passing including 5 full-pipeline e2e tests.

## What Worked

- **Spike-verify pattern**: RF-12 spiked the -1 sentinel approach before committing to it. All 10 test cases passed in the spike, and the implementation was a direct transcription.
- **Parallel task execution**: P1.1 (comparison operators) and P2.1 (chunk_text_hash) ran concurrently — different files, no conflicts.
- **Two-phase code review**: P1 review caught commutativity test gap; P2 review caught defense-in-depth gap (chash re-validation). Both were quick fixes.
- **Substantive critique found real bugs**: `resolve_span_text()` missing chash: branch and `stale_spans` false positives for chash: links — neither caught by unit tests alone.
- **Code review caught the streaming pipeline gap**: `pipeline_stages.py` was missing `chunk_text_hash`, which would have silently broken chash: links for streamed PDFs.

## What Didn't Work

- **Premature RDR status update**: Set `status: closed` in frontmatter before the formal rdr-close process. Should have left as `accepted` and let the close workflow handle it.
- **Test isolation**: `_EPHEMERAL_T3` module-level state caused cross-test ordering failures in the e2e suite. Fixed by switching to per-test `EphemeralClient()` with `tmp_path.name` suffixes.
- **5th indexer missed on first pass**: The streaming PDF pipeline (`pipeline_stages.py`) was not in the enriched bead description for nexus-tvyv, so it was missed until the code review caught it.

## Deviations Accepted

7 deliberate deviations from Nelson's Xanadu documented in deviations register (D1-D7). Key decisions:
- D1: Fixed-depth tumblers (not Nelson's variable-depth) — revisit when ADD/SUBTRACT needed
- D5: Position-based chunk spans coexist with content-hash spans
- D6: Simplified integer comparison, not transfinitesimal arithmetic

## Metrics

- Research findings: 15 (RF-1 through RF-15, all verified)
- Beads: 8 tasks + 1 epic, all closed
- Tests added: ~50 new tests (unit + e2e)
- Files modified: 10 implementation + 5 test + 7 doc files
- Review rounds: 2 (P1 review + P2 review) + 1 substantive critique + 1 code review
- Bugs found by critique/review: 5 (all fixed before merge)
