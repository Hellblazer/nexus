---
title: "MinerU-Only PDF Extraction with a Per-Page Fallback Ladder"
id: RDR-237
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-27
accepted_date:
related_issues: [nexus-kh7zj, nexus-73pvi, nexus-lqaq1]
related_rdrs: [RDR-021, RDR-044, RDR-048, RDR-148]
supersedes: [RDR-044]
---

# RDR-237: MinerU-Only PDF Extraction with a Per-Page Fallback Ladder

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

## Problem Statement

Nexus turns a PDF into text before it chunks and embeds it. That step is
called extraction, and nexus has three extractors: **Docling** (IBM's
layout-aware converter, run inside the indexing process), **MinerU** (a
layout and formula model, run in a child process or as a local HTTP
server), and **PyMuPDF** (the PDF library, which reads the text layer a PDF
already carries). Since RDR-044 the default, `auto`, runs Docling on every
PDF and switches to MinerU only when it decides the PDF contains math.

On 2026-10-26 and 2026-10-27 Sam named three failures, all traced to
Docling: extraction blocks for hours for reasons nobody can see, the parse
of math and scanned papers is bad, and when extraction fails there is no
fallback. He chose to remove Docling and make MinerU the only extractor,
with a fallback that works one page at a time so a document always
finishes. This RDR designs that.

### Enumerated gaps to close

#### Gap 1: Extraction time is unbounded, and a long run is silent

Docling's formula enrichment (a generative model it runs over every formula
region, here on the CPU) has no time limit. One 19-page paper ran 9 h 40 min
and never finished (nexus-73pvi). Docling also converts the whole document in
one call, so nothing is reported until it ends. The pipeline row's heartbeat
moves only when progress is written, so a run silent for more than 5 minutes
looks dead: a retry, a second `nx dt index`, or a peer then takes the row over
and the live run loses its work (nexus-lqaq1).

#### Gap 2: `auto` sends scanned math papers to the wrong extractor

`auto` decides "is this math?" by counting math symbols in the PDF's existing
text layer. A scanned paper's text layer is old OCR with no math symbols, so
every scanned math paper is routed to Docling (nexus-kh7zj).

#### Gap 3: Docling's output on math and scans is poor

Docling garbles equations in scanned papers beyond recovery, where MinerU
recovers most of them as exact LaTeX, and MinerU's output retrieves better
(RF-1).

#### Gap 4: A failure stops the document; nothing degrades gracefully

A MinerU failure in `auto` stops the document with "rerun with
`--extractor docling`". The only automatic degrade (`on_formula_oom=docling`,
RDR-148 Gap 5) sends a page whose formula model ran out of memory to Docling.
The PyMuPDF text fallback runs only after a Docling failure. With Docling gone,
nothing would remain between "fully extracted" and "failed".

#### Gap 5: The MinerU server path has no real per-page bound

The MinerU server serves one request at a time and runs each parse in a
thread it cannot cancel. Nexus gives up on a server request after a fixed
300 s, not the configured per-page timeout, and then retries the same page on
the server before falling back to the killable subprocess path (RF-3). One
stuck page can therefore hold the run for about 13 minutes with no progress
written, past the 5-minute stale window of Gap 1.

#### Gap 6: Two extractors double the install, the CI and the test surface

Docling brings its own models (about 0.5 GB on first PDF, 1.1 GB with formula
enrichment), its own pins, a CI cache-priming action and a non-vacuity step,
a doctor row, and about 465 test lines across 67 files (RF-6). Every one of
them is maintained for the extractor Sam has ruled out.

## Relationship to Prior RDRs

Searched the RDR index for docling, mineru, pdf extraction, formula, and OCR.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-044 Math-Aware PDF Extraction | Superseded | It made Docling the default with MinerU for math, on the rationale "zero overhead for non-math papers". That rationale assumed the formula screen routes correctly and Docling finishes; Gaps 1 and 2 show neither holds. It listed "MinerU as primary for all PDFs" as an alternative, conditional on MinerU's non-math quality matching Docling's, which A3 tests. |
| RDR-021 Docling PDF Extraction | Origin (closed) | Introduced Docling. Nothing in it survives this RDR. |
| RDR-148 MinerU formula OOM | Precedent | Its Gap 5 added the per-page degrade to Docling. This RDR keeps the per-page shape and replaces the rung below MinerU. |
| RDR-048 streaming PDF pipeline | Precedent | It wanted pages to stream from the extractor. MinerU's per-page batches already do; Docling's whole-document convert was the exception. |

## Context

### Background

Evidence came from the ART bulk-index session (Sam's papers in DEVONthink),
from the laptop and chas measurements cited in Research Findings, and from
reading `src/nexus/pdf_extractor.py` and the installed MinerU 3.1.11 source.
Sam's decision, in his words: "screw docling and just use mineru. this has
cost way more time than it ever could possibly save" (2026-10-26), and on the
design below, "I think this is the best compromise" (2026-10-27).

### Technical Environment

- `src/nexus/pdf_extractor.py`: `PDFExtractor.extract()` and its router
  `_extract_dispatch()`; `_extract_with_mineru()`, which splits pages into
  batches of `pdf.mineru_page_batch` (default 1) and runs each through
  `_mineru_run_isolated()`: the HTTP server when one is up, else a child
  process (`_mineru_run_subprocess`), which bisects a failing range down to
  one page.
- Config (`PdfConfig`, `src/nexus/config.py`): `pdf.extractor`
  (`auto`/`docling`/`mineru`), `mineru_server_url`, `mineru_autostart`,
  `mineru_table_enable`, `mineru_page_batch`, `mineru_memory_ceiling_mb`,
  `mineru_page_timeout_s` (180).
- Pipeline heartbeat: `pdf_pipeline.updated_at`, stamped by the engine on each
  progress flush; a `running` row older than 5 minutes is taken over
  (`PipelineRepository.STALE_THRESHOLD`, mirrored client-side).
- MinerU 3.1.11 (`mineru[pipeline]`, pinned `>=3.1.11,<3.1.12`).

## Research Findings

### Investigation

Source read in the worktree at develop 9628991be: `pdf_extractor.py`,
`pdf_stack.py`, `health.py`, `config.py`, `commands/index.py`,
`commands/dt.py`, `pipeline_stages.py`, `db/http_pipeline_client.py`; the
installed `mineru/cli/fast_api.py` (3.1.11). Measurements are cited from the
beads and T2 records named in each finding; none were re-run for this draft.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| MinerU 3.1.11 `do_parse` | Yes | Takes `formula_enable` and `table_enable` per call (nexus's worker passes both, `pdf_extractor.py:137-138`). |
| MinerU 3.1.11 `/file_parse` server | Yes | `formula_enable` is a per-request form field (`fast_api.py:839`, passed to the parse at :1023). Requests are serialized by an `asyncio.Semaphore` of 1 (`:103-104`, `:260`); the parse runs in `asyncio.to_thread(do_parse)` (`:1042`), which cannot be cancelled. |
| PyMuPDF | Yes | `page.get_text()` per page is what the formula screen and `_extract_normalized` already use. |

### Key Discoveries

**RF-1 (Verified, nexus-kh7zj, ART 2026-10-26).** Grossberg & Stone 1986, a
30-page scanned paper with 20 numbered equations: the formula screen counted 0
math symbols in a 186,699-character text layer (threshold 5), so `auto` chose
Docling, which garbled the math. `--extractor mineru` took 203 s (about 7 s
per page), lost no page, and gave 17 of 20 numbered equations as exact LaTeX.
On 12 questions scored by page, Docling had MRR 0.65 and MinerU 0.74.

**RF-2 (Verified, nexus-73pvi and nexus-lqaq1).** Docling with formula
enrichment ran 9 h 40 min on a 19-page paper (CPU, KV cache off, no time limit,
no progress). Unenriched Docling on a 300-page PDF was silent 83.7 s, then
replayed all 300 pages in 0.1 s: about 0.28 s per page on the laptop. MinerU per
page: first page 16.5 s, largest gap 22.4 s on a 19-page PDF.

**RF-3 (Documented, source).** On the server path, nexus posts each batch with
a fixed `httpx` timeout of 300 s (`pdf_extractor.py`, `_mineru_run_via_server`),
not `mineru_page_timeout_s`. On a timeout it restarts the server, retries the
same batch on the new server, then falls back to the subprocess path
(`_mineru_run_isolated`). The subprocess path bounds a batch at
`mineru_page_timeout_s` × pages and kills the worker's process group. The MinerU
server keeps parsing a page after nexus gives up, holding its single slot
(Dependency table).

**RF-4 (Verified, T2 `nexus/mineru-server-memory-repeat-extraction-2026-10-12`).**
On chas, CPU only, a 30-page PDF through the MinerU server 12 times: 168 to 206 s
per pass (about 6 s per page), server memory flat at about 3.1 to 3.3 GB after
the first pass.

**RF-5 (Verified, T2 `nexus/mineru-formula-oom-rdr148-gap5-recheck-2026-10-11`).**
The page that reproducibly killed MinerU's formula model (RDR-148 Gap 5) did not
kill it on Sam's 128 GB Mac with MinerU 3.1.11; the whole 43-page paper
extracted in 267 s with server memory peaking at 5.9 GB. Not checked on Linux or
a smaller machine.

**RF-6 (Documented, source inventory 2026-10-27).** Docling's footprint: imports
only in `_get_converter`; routing, degrade and fallback code in
`pdf_extractor.py`; `pdf_stack.STACK_DISTS`/`STACK_MODULES`; the doctor row
`PDF extraction (docling)`; `pdf.extractor` and three `--extractor` CLI choices
(`nx index pdf`, `nx dt index`, `nx dt capture`); four pins in
`_install/constraints.txt`; the `docling-slim` declaration in the `pdf` extra;
CI's `prime-docling` action and `Verify docling extracts` steps; about 465
matching test lines in 67 files; install and size text in README, `web/`,
`mcpb/`, `conexus/` setup. Title resolution reads `docling_title` first
(`indexer_utils.py`, `pipeline_stages.py`). The `cli-reference.md` claim that
`auto` "returns the Docling result anyway" without MinerU is already false; the
code raises.

**RF-7 (Verified, T2 `nexus/install-weight-measurement-2026-10-23`).** Of 1,605 MB
installed, 792 MB is used only by MinerU, Docling and torch together. Docling's
own share was not measured; MinerU also needs torch.

**RF-8 (Verified, source; T2 `nexus_rdr/237-research-1`).** `_extract_normalized`
already reads and normalizes each page on its own (`page.get_text(sort=True)`
in a per-page loop over `pymupdf.open`), so the last rung for one page is the
same call on one page.

**RF-9 (Verified, source; T2 `nexus_rdr/237-research-2`).** Walking `uv.lock` from
the conexus root with every extra and dev group, 14 packages are reachable only
through `docling-slim`: accelerate, doclang, docling-core, docling-ibm-models,
docling-parse, docling-slim, jsonref, langcodes, latex2mathml, psutil, rtree,
shellingham, tabulate, typer. No module in `src/nexus`, `scripts`, `tests` or
`conexus` imports any of them. pypdfium2, pillow and torch stay, through
MinerU. The pillow cap, whose stated reason is `docling-slim`, may become
relaxable once it is gone.

### Critical Assumptions

- [ ] A1: MinerU honours `formula_enable=false` per request on the server path
  and per call in the subprocess worker, and the rest of the page (text, layout,
  tables) still extracts. — **Status**: Unverified — **Method**: Source Search
  done (the parameter exists on both paths); Spike needed.
- [ ] A2: Restarting the MinerU server frees a slot held by a stuck page, so the
  next page is not queued behind it. — **Status**: Unverified — **Method**: Spike.
- [ ] A3: MinerU's output on non-math, born-digital PDFs is at least as good for
  retrieval as Docling's. RF-1 covers one scanned math paper only. —
  **Status**: Unverified — **Method**: Spike (retrieval comparison on non-math
  PDFs).
- [x] A4: PyMuPDF can return the text layer of one page of a PDF in isolation,
  quickly, for use as the last rung. — **Status**: Verified (RF-8) — **Method**:
  Source Search.
- [x] A5: Removing `docling-slim` removes nothing another part of nexus imports
  (pillow and pypdfium2 pins, `cross_encoder` comment). — **Status**:
  Verified (RF-9) — **Method**: Source Search (a reachability walk of `uv.lock`).

## Proposed Solution

### Approach

Make the page the unit of work and the unit of failure. Every page goes
through MinerU under a real time limit. A page that fails moves down a ladder
of cheaper rungs until something returns text, and the document always
finishes. The run names each degraded page so it can be re-extracted later.

The ladder, per page:

1. **MinerU** with formulas and tables (today's MinerU extraction).
2. **MinerU without formulas** (`formula_enable=false`) for that page alone:
   keeps text, layout and tables, loses that page's equations. This replaces
   RDR-148's degrade to Docling and covers formula out-of-memory kills.
3. **The page's text layer** through PyMuPDF, stamped degraded: no layout,
   tables or math, but the words are searchable.
4. **Nothing**: a page with no text layer (an image-only scan) whose earlier
   rungs failed is recorded as unextracted, not silently dropped.

Docling is removed: code, dependency, configuration value, CLI choice, doctor
row, CI steps, docs and tests.

### Technical Design

**Bounds.** Each rung has a time limit, and the subprocess path is the only one
that can actually stop work, so it is the path a timed-out page retries on:

- the server request timeout becomes `mineru_page_timeout_s` × pages in the
  batch (today a fixed 300 s, RF-3);
- a server timeout restarts the server (A2) and sends that page to the next rung
  on the subprocess path, never the same rung again on the server;
- `mineru_page_timeout_s` must stay under the 5-minute stale window, so that one
  page's limit cannot outlast the heartbeat (validated at config load).

The worst case for a document is bounded by pages × the sum of the rung limits,
and is no longer unbounded.

**Progress.** Each page writes progress when it settles, whichever rung settled
it. With one page per batch and the per-page limit under the stale window, a
live run always writes progress more often than the takeover window allows.
This closes nexus-lqaq1 for the remaining extractor without an engine heartbeat
route.

**What each page records.** The page's `extraction_method` takes one of
`mineru`, `mineru-no-formula`, `pymupdf-text` or `unextracted`. The document
summary counts each kind and the run output names degraded pages. Existing
values (`docling`, `mineru+docling-degraded`, `pymupdf_normalized`) stay
readable on documents already indexed.

**Configuration and CLI.** `pdf.extractor` and `--extractor` accept `mineru`.
The values `auto` and `docling`, still present in users' configs and scripts,
mean `mineru` and print a one-line deprecation notice. `--on-formula-oom` is
retired, since the ladder handles that case. Whether to add an explicit
`--extractor pymupdf` (text layer only, for a host that cannot run MinerU) is
open question Q1.

**No MinerU at all.** If MinerU is not installed, extraction fails at once and
names the fix (`pdf_stack` already words this), rather than degrading every
page to its text layer. Q1 decides whether `pymupdf` exists as an explicit
choice for that case.

**Title.** Title resolution falls back from `docling_title` to the next source
already in the chain (`pdf_title`, then filename); documents that carry a
`docling_title` keep it.

```text
// Illustrative: the per-page outcome the ladder returns
PageOutcome(page: int, text: str, method: "mineru" | "mineru-no-formula"
            | "pymupdf-text" | "unextracted", seconds: float, error: str | None)
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Per-page MinerU run | `_extract_with_mineru`, `_mineru_run_isolated`, `_mineru_run_subprocess` (bisect to one page) | Reuse; the ladder wraps the single-page settle. |
| Rung 2 | `_degrade_page_to_docling` (RDR-148 Gap 5) | Replace: same one-page shape, MinerU with formulas off. |
| Rung 3 | `_extract_normalized` (whole-document PyMuPDF) | Extend to one page. |
| Server timeout and recovery | `_mineru_run_via_server`, `_restart_mineru_server` | Extend: per-page timeout; a timed-out page leaves the server for the next rung. |
| Missing-stack message | `pdf_stack.require` / `missing_message` | Reuse; drop Docling from `STACK_DISTS`/`STACK_MODULES`. |
| Doctor | `health._check_pdf_support`, `_check_docling_importable` | Remove the Docling row; keep the PDF-support and MinerU rows. |
| Quality gate | `PDFExtractor.extract` gate | Reuse; run per document as today. |

### Decision Rationale

The ladder is Sam's compromise: one extractor whose failures cannot block or
sink a document. MinerU alone, without the ladder, would fix Gaps 1 to 3 but
turn every page failure into a document failure. Keeping Docling as a fallback
rung would keep Gaps 1 and 6. A text-layer rung keeps something for every page
that has a text layer at almost no cost.

## Alternatives Considered

### Alternative 1: Keep `auto`, fix the formula screen

**Description**: route scans (OCR text layers, image-only pages) to MinerU,
put a time limit on Docling, and add a Docling-to-MinerU fallback (the asks in
nexus-kh7zj and nexus-73pvi).

**Pros**: non-math born-digital PDFs keep Docling's speed (about 0.28 s per page).

**Cons**: three fixes to an extractor Sam has ruled out; two extractors stay in
the install, CI and tests; Docling's whole-document convert stays silent.

**Reason for rejection**: Sam's decision, and Gaps 1 and 6 remain.

### Alternative 2: MinerU only, no ladder

**Description**: replace Docling with MinerU and fail a document on any page failure.

**Pros**: smallest change.

**Cons**: a single bad page fails the whole document, the "no fallback" pain Sam named.

**Reason for rejection**: does not close Gap 4.

### Briefly Rejected

- **Automatic text-layer fallback for the whole document when MinerU is absent**:
  hides a broken install behind a quietly worse index; the run would succeed
  with no layout, tables or math.
- **Re-extract every Docling-extracted document under MinerU**: about 6 to 7 s
  per page across a whole library, unasked for; `nx index pdf --force` does it
  for any document a user chooses.

## Trade-offs

### Consequences

- Extraction is slower for non-math, born-digital PDFs: about 6 to 7 s per page
  on CPU (RF-1, RF-4) against about 0.28 s (RF-2). A 300-page book goes from about
  1.5 minutes to about 35, with steady progress instead of silence. Sam accepted
  this cost.
- Math and scans extract better (RF-1), and no routing guess is made.
- A document always finishes; degraded pages are named and countable.
- The install loses Docling and its models; CI loses a priming action and a step.

### Risks and Mitigations

- **Risk**: MinerU's non-math output is worse than Docling's for some PDFs (A3).
  **Mitigation**: the A3 spike before acceptance; if it fails, revisit with Sam.
- **Risk**: `formula_enable=false` does not behave as expected (A1).
  **Mitigation**: the A1 spike; without it, rung 2 is dropped and rung 3 takes
  formula failures.
- **Risk**: users scripted `--extractor docling` or `--on-formula-oom docling`.
  **Mitigation**: `docling` maps to `mineru` with a notice; `--on-formula-oom`
  stays accepted and ignored for one release, with a notice.

### Failure Modes

- MinerU absent: the run fails at once with the install command (visible).
- A page settles on rung 3 or 4: the run output names it and the document
  summary counts it (visible); the page is searchable by its text layer or not at all.
- The MinerU server wedges: the page times out, the server restarts, the page
  moves to the subprocess path (logged with the page number).

## Implementation Plan

### Prerequisites

- [ ] A1 to A5 verified.
- [ ] Q1 answered by Sam.

### Minimum Viable Validation

On the laptop, with Docling uninstalled, index three PDFs end to end: the
Grossberg & Stone 1986 scan (RF-1), the 19-page paper that hung Docling for
9 h 40 min (nexus-73pvi), and the 300-page TCoDEVONthink manual (RF-2). Each
finishes within its page bound, writes progress at least once a minute, keeps
the equations RF-1 found, and names any degraded page. Repeat one with a page
forced to fail on rungs 1 and 2 to show rung 3 settle it.

### Phase 1: Code Implementation

#### Step 1: The ladder

Per-page outcome type; rung 2 (MinerU without formulas) and rung 3 (one-page
PyMuPDF text); per-page `extraction_method`; summary counts and named pages.
Tests first, with a fake MinerU that fails on chosen pages and rungs.

#### Step 2: Bounds

Server timeout from `mineru_page_timeout_s`; a timed-out page goes to the next
rung on the subprocess path; config validation keeps the page limit under the
stale window.

#### Step 3: Remove Docling

Router, Docling code paths, `pdf_stack` entries, doctor row, constraints pins,
the `pdf` extra entry, CI priming and verify steps, `--on-formula-oom`; `auto`
and `docling` become aliases with a notice; title fallback; docs and size text;
tests (delete Docling tests, retarget routing tests to the ladder).

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Per-page `extraction_method` on chunks | N/A | In scope: the document summary and `nx store get` show it | N/A | In scope: doctor or a query counts degraded pages | N/A |

### New Dependencies

None. Docling (`docling-slim` and its three companions) is removed.

## Test Plan

- **Scenario**: every page succeeds on MinerU — **Verify**: all pages `mineru`,
  no degraded names.
- **Scenario**: one page fails on rung 1 — **Verify**: it settles `mineru-no-formula`,
  others `mineru`, the page is named.
- **Scenario**: one page fails on rungs 1 and 2 — **Verify**: `pymupdf-text`.
- **Scenario**: an image-only page fails everywhere — **Verify**: `unextracted`,
  named, the document still finishes.
- **Scenario**: a server request times out — **Verify**: the server restarts,
  the page retries on the subprocess path at the next rung, not on the server.
- **Scenario**: MinerU not installed — **Verify**: immediate failure naming the fix,
  no page extracted.
- **Scenario**: `pdf.extractor: docling` in config — **Verify**: runs MinerU, one notice.
- **Scenario**: `mineru_page_timeout_s` set to the stale window or more —
  **Verify**: refused at config load.
- **Scenario**: a long document — **Verify**: progress written at least every page.

## Validation

### Testing Strategy

Storage-free unit tests drive the ladder with a fake MinerU. The engine-backed
pipeline tests (`test_rdr223_pdf_journey` and peers) prove progress and the
takeover window. The MVV runs real MinerU on the three named PDFs.

### Performance Expectations

The per-page costs are RF-1, RF-2 and RF-4; this RDR measures the three MVV
documents on the laptop and chas before acceptance.

## Finalization Gate

### Contradiction Check

To be completed at gate.

### Assumption Verification

To be completed at gate (A1 to A5).

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `do_parse(..., formula_enable=...)` | MinerU 3.1.11 | Source Search (spike pending, A1) |
| `POST /file_parse` `formula_enable` | MinerU 3.1.11 | Source Search (spike pending, A1) |
| `page.get_text()` | PyMuPDF | Source Search (A4) |

### Scope Verification

The MVV is in scope and runs before the RDR closes.

### Cross-Cutting Concerns

- **Versioning**: `auto`/`docling` aliases and a retired `--on-formula-oom`, each
  with a notice for one release.
- **Build tool compatibility**: N/A.
- **Licensing**: removing a dependency only.
- **Deployment model**: client change; no engine change.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Docling-extracted documents stay; re-extraction is opt-in.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: MinerU server memory per RF-4; Docling's in-process
  converter (about 2 GB) leaves the indexing process.

### Proportionality

Right-sized for a change that removes a dependency across code, CI and docs.

## Open Questions for Sam

- **Q1**: should `--extractor pymupdf` (text layer only, no layout, tables or
  math) exist as an explicit choice for a host that cannot run MinerU, or does
  a missing MinerU always fail?

## References

- `src/nexus/pdf_extractor.py` (router, MinerU paths, Docling paths, degrade)
- `src/nexus/pdf_stack.py`, `src/nexus/health.py`, `src/nexus/config.py`
- `src/nexus/pipeline_stages.py`, `src/nexus/db/http_pipeline_client.py`,
  `service/src/main/java/dev/nexus/service/db/PipelineRepository.java`
- MinerU 3.1.11 `mineru/cli/fast_api.py`, `mineru/cli/common.py`
- Beads nexus-kh7zj, nexus-73pvi, nexus-lqaq1
- T2: `nexus/mineru-server-memory-repeat-extraction-2026-10-12`,
  `nexus/mineru-formula-oom-rdr148-gap5-recheck-2026-10-11`,
  `nexus/install-weight-measurement-2026-10-23`

## Revision History

- 2026-10-27: draft.
