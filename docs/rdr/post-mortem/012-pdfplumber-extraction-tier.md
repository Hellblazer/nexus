---
rdr: RDR-012
title: "Post-Mortem: pdfplumber Extraction Tier"
date: "2026-03-01"
author: Hal Hildebrand
prs: ["#50 (initial implementation)", "#51 (layout bug fix)"]
---

# Post-Mortem: RDR-012 — pdfplumber Extraction Tier

## Summary

RDR-012 was implemented in two PRs across a single session. The initial implementation
(PR #50) matched the design plan but contained a latent process-global state bug:
`pymupdf.layout.activate()` — called inside `_extract_markdown()` — permanently suppresses
`find_tables()` on some PDFs for the remainder of the Python process. This meant that in
any session processing more than one PDF, the table quality check was unreliable for all
PDFs after the first. The bug was discovered only during manual corpus verification
(not by the test suite), required a second PR (#51) to fix, and exposed a gap in the
RDR workflow: the RDR was accepted and initially closed before real-world verification
was complete.

---

## What Was Planned vs. What Was Implemented

### Planned (RDR-012 Implementation Plan)

| Phase | Description | Status |
| --- | --- | --- |
| 1 | Add pdfplumber dependency + `_normalize_whitespace_edge_cases()` | Done as designed |
| 2 | Add `_markdown_misses_tables()` using PyMuPDF `find_tables()` | Done, but algorithm flawed (see below) |
| 3 | Add `_extract_with_pdfplumber()` with prose/table deduplication | Done as designed |
| 4 | Wire into `extract()` | Done as designed |
| 5 | Tests (unit + subsystem) | Done — all 1874 passed at merge |

### What Actually Shipped

Phase 2 was partially wrong. The RDR specified:

> "Uses PyMuPDF's native `find_tables()` (same algorithm as pdfplumber) to count ruled
> tables on the first five pages"

The implementation called `_markdown_misses_tables(pdf_path, result.text)` from `extract()`
**after** `_extract_markdown()` had already activated layout mode. `pymupdf.layout.activate()`
is a process-global call; once invoked, it suppresses `find_tables()` on some PDFs
(confirmed: `distributed-bloom-filter.pdf` went from 5 detected tables → 0 after activation).

The fix in PR #51:
- Added `_count_ruled_tables(pdf_path)` using **pdfplumber's** `find_tables()` (pdfminer-based,
  immune to pymupdf layout state)
- Refactored `_markdown_misses_tables(count: int, text: str)` into a pure predicate
- Called `_count_ruled_tables()` at the top of `extract()`, before `_extract_markdown()`

---

## Root Cause Analysis

### The Bug: `layout.activate()` Is Process-Global

`pymupdf.layout.activate()` enables a neural network–based layout analysis engine.
It is a **singleton with no deactivation path**: once called in a Python process,
it permanently changes the behavior of `page.find_tables()` on some PDFs (likely those
where the layout engine reclassifies table edges as body text regions).

The RDR specified "Uses PyMuPDF's native `find_tables()`" with a note that pdfplumber
uses the same algorithm. That assumption was correct in isolation. But the RDR did not
account for the interaction between `layout.activate()` (called in Tier 1) and the
table detection logic (called between Tier 1 and Tier 2 decisions).

### Why Tests Didn't Catch It

All unit tests for `_markdown_misses_tables` mocked pymupdf — they never exercised the
real `find_tables()` interaction. The subsystem test (`test_pdfplumber_tier_fires_for_table_pdf`)
used a synthetic ruled-table PDF created fresh per test, so layout state never accumulated.
No test exercised the scenario of: **process multiple PDFs sequentially, then verify
that table detection still works for later PDFs**.

### Discovery Path

The bug was found during manual verification: running all 19 papers in the delos corpus
through `extract()` in a single Python process. `distributed-bloom-filter.pdf` (5 ruled
tables on pages 2 and 4) returned `method=pymupdf4llm_markdown, pipes=0` — tables
completely missed. Isolating the PDF showed 5 tables; adding `layout.activate()` first
showed 0. The root cause was immediately clear.

---

## Timeline

| Event | PR / Commit |
| --- | --- |
| RDR-012 created, researched, gated, accepted | docs commits |
| Phase 1–5 implementation | PR #50 (squash: `a547ce9→d052771` initially was `a547ce9`) |
| RDR marked closed, post-mortem deferred | `docs(rdr): close RDR-012` |
| Manual corpus verification against 19 papers | (in-session, post-merge) |
| Bug discovered: layout suppresses find_tables() | — |
| Fix: switch to pdfplumber-based counting + pure predicate | PR #51 (`d052771`) |
| Full corpus re-verified, all 19 papers correct | — |

---

## Divergences from RDR Design

| Area | RDR Design | Actual Implementation |
| --- | --- | --- |
| Table detection engine | PyMuPDF `find_tables()` | pdfplumber `find_tables()` (immune to layout state) |
| `_markdown_misses_tables` signature | `(pdf_path, markdown_text)` | `(ruled_table_count: int, markdown_text: str)` — pure predicate |
| Table counting location | Inside `_markdown_misses_tables` | Extracted to `_count_ruled_tables()`, called first in `extract()` |
| False-positive behavior | Only ruled-border tables detected | pdfplumber detects ResearchGate cover page headers as tables (acceptable tradeoff) |

---

## Process Lessons

### 1. Real-Corpus Verification Must Gate Closure

The RDR was accepted and marked closed after: unit tests pass, subsystem tests pass,
PR merged. But **no real PDFs were run through the extraction pipeline** before closure.
The bug only appeared when processing a corpus sequentially in one process.

**Process change**: Add "verified against real-world corpus" as a required step before
`/rdr-close` for any extraction-tier RDR. For RDR-012 specifically, this would have
been: run all delos-papers through `extract()` in a single process, check each
extraction_method and pipe count.

### 2. Multi-Item In-Process Testing Must Be in the Test Plan

Process-global state bugs (singletons, module-level activation, cached engine state)
are invisible to tests that exercise one item in isolation. The RDR test plan (Phase 5)
specified:

> "test_pdfplumber_tier_fires_for_table_pdf: table PDF with deficient pymupdf4llm output
> → final metadata has extraction_method == 'pdfplumber'"

This test used a synthetic PDF and ran a single `extract()` call. It passed. But
no test ran two `extract()` calls sequentially and verified that the second one still
detected tables correctly.

**Process change**: For any RDR that involves lazy imports, module-level initialization,
or framework state, the test plan must include at least one test that processes **multiple
items in sequence** in the same process and verifies that state does not accumulate
incorrectly.

### 3. The RDR Workflow Should Distinguish "Implementation Complete" from "Verified Correct"

The current workflow: Draft → Gate → Accept → Implement → Close.
"Close" currently means "implemented and PR merged." But as this case shows, a passing
test suite does not equal correct real-world behavior.

**Proposed addition**: A "Verification" phase between implementation and closure:
- Run against real representative inputs (not just synthetic test fixtures)
- For extraction tiers: process a full corpus in one session
- For search features: run live queries and inspect results
- Document the verification evidence in the close commit or post-mortem

This does not need to be a formal gate (that would slow small RDRs). It should be
a **checklist item in `/rdr-close`** that asks: "Has this been verified against
real-world inputs?"

### 4. Fixture PDFs Should Be Added During Research, Not After

The delos-papers corpus existed but was not added to `tests/fixtures/` until
the post-implementation investigation. Had representative PDFs been added during
Phase 1 (or even during the Research phase), the subsystem test could have used
them, and the multi-PDF scenario would have been testable from the start.

**Process change**: When an RDR involves a data-processing pipeline (PDF extraction,
chunking, indexing), add representative real-world samples to `tests/fixtures/` during
the Research phase. Reference them in the Implementation Plan's test section.

---

## What Went Well

- The design logic was correct: attempt-first, quality-rescue, graceful degradation.
- The `_extract_with_pdfplumber()` implementation was solid on the first attempt.
- The fix (PR #51) was clean and small: 3 files, 180 lines, no regressions.
- The bug was caught before any real indexing ran (no user data was affected).
- Splitting `_markdown_misses_tables` into a pure predicate + `_count_ruled_tables` is
  strictly better architecture than the original design.

---

## Artifacts

- **PR #50**: Initial implementation — `a547ce9` on main (squash)
- **PR #51**: Layout activation fix — `d052771` on main (squash)
- **Test files**: `tests/test_pdf_extractor.py`, `tests/test_pdf_subsystem.py`
- **Fixture PDFs**: `tests/fixtures/` (gitignored; copy from `~/Downloads/delos-papers/`)
- **Implementation**: `src/nexus/pdf_extractor.py`
