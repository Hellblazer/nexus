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

The MinerU server runs each parse in a thread it cannot cancel, and serves one
request at a time on macOS and three elsewhere by default (Dependency table).
Nexus gives up on a server request after a fixed
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
| MinerU 3.1.11 `/file_parse` server | Yes | `formula_enable` is a per-request form field (`fast_api.py:839`, passed to the parse at :1023). Concurrent requests are capped by an `asyncio.Semaphore` whose size is 1 on macOS and `DEFAULT_MAX_CONCURRENT_REQUESTS` (3, `api_protocol.py:3`) elsewhere unless `MINERU_API_MAX_CONCURRENT_REQUESTS` is set (`fast_api.py:250-260`); the parse runs in `asyncio.to_thread(do_parse)` (`:1042`), which cannot be cancelled. |
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

**RF-10 (Verified, spike; T2 `nexus_rdr/237-research-3`).** On chas (CPU only,
MinerU 3.1.11), pages 1 to 3 of `virgo.pdf` one at a time, with the formula model
on and then off. Page 3, which carries the equations, went from 129.1 s to 37.1 s
on the subprocess path and from 21.2 s to 3.9 s on the server, from 4,855 to 4,503
characters, and from 10 formula markers to 0. Pages without equations kept their
text. The switch works per call on both paths and drops only the equations. A
cold subprocess page with formulas took 129 s, close to the 180 s page timeout.

**RF-11 (Verified by proxy, spike; T2 `nexus_rdr/237-research-4`).** Word recall
and precision against each PDF's own text layer (ground truth for a born-digital
PDF), counting words of three or more letters:

| PDF | Pages | Docling recall / precision / time | MinerU recall / precision / time |
| --- | --- | --- | --- |
| `tests/fixtures/tc-sql.pdf` | 30 | 0.864 / 0.824 / 9,397 s | 0.845 / 0.736 / 297 s |
| `papers/2408.04948.pdf` | 9 | 0.955 / 0.962 / 12.9 s | 0.951 / 0.953 / 35.8 s |

Recall is within two points. MinerU writes tables as HTML, which likely lowers its
precision on this metric through tag words. Docling's default (enriched) path took
2.6 hours on a 30-page PDF with no math, the hang of nexus-73pvi on an ordinary
document. This is a text-completeness proxy, not a retrieval evaluation.

**RF-12 (Verified, spike; T2 `nexus_rdr/237-research-5`).** A client that gave up on
a six-page server request at 3, 10 or 30 s did not delay the next one-page request
(17.8 s, 23.1 s and 25.1 s, against warm one-page baselines of 18.9 s and 33.5 s).
The server log shows the abandoned job ran to completion: MinerU finishes abandoned
work rather than cancelling it. chas is Linux, so this server had three slots
(Dependency table); the single-slot macOS case was not run. A page that never
finishes was not tested and would hold one slot until a restart. A server restart
cost about 30 s of model reload (the next page took 48.5 s against 17.8 s).

**RF-13 (Verified, source; T2 `nexus_rdr/237-research-6`).** The engine's
`PipelineRepository.updateProgress` sets `updated_at` to now for any non-empty
field map (`PipelineRepository.java:395-420`), so re-sending an unchanged
`pages_extracted` stamps the heartbeat; the client's `update_progress` flushes at
once when no page or chunk batch is pending (`http_pipeline_client.py:378-392`).

**RF-14 (Verified, source; T2 `nexus_rdr/237-research-7`).** `extraction_method` is
one document-level string, set in `_mineru_build_result` (`pdf_extractor.py:3007`)
and merged onto every chunk (`pipeline_stages.py:2134`); `nx store get` prints it
as `Extractor:` (`commands/store.py:540-550`). Chunks are content-addressed, so a
per-page value cannot live on them. The catalog writer's `update` merges `meta`
(`store_hook.py:1001-1003`), and the PDF catalog hook already updates the
document row (`pipeline_stages.py:1004`).

**RF-15 (Verified, source; T2 `nexus_rdr/237-research-8`).** Today's single-page
retry catches only `RuntimeError` (`pdf_extractor.py:2076`); `MineruMemoryError`
is a `RuntimeError` (`:215`), `MineruMachineResourceError` is a plain `Exception`
(`_mineru_health.py:65`) and `MineruWorkerStopped` a `BaseException` (`:165`).
The quality gate passes an empty text without judging it
(`pdf_extractor.py:1273-1281`). The Docling doctor row is the only one that checks
`cv2`, `torch` and `torchvision` (`health.py:2774-2776`). Six call sites require
or install the PDF stack before any extractor runs (`pdf_extractor.py:1510`,
`doc_indexer.py:2742`, `commands/index.py:3177` and `:3221`, `commands/dt.py:1098`
and `:1990`). The page count comes from `len(pdf_info)` (`pdf_extractor.py:2957`).
`git grep -i docling` finds it in five `.github` files, seven under `scripts/`
(outside `scripts/spikes` and `scripts/experiments`), 31 under `src/`, nine docs
outside `docs/rdr`, three under `conexus/`, `mcpb/manifest.json`, three under
`web/`, `README.md`, `pyproject.toml`, and 68 test files.

### Critical Assumptions

- [x] A1: MinerU honours `formula_enable=false` per request on the server path
  and per call in the subprocess worker, and the rest of the page (text, layout,
  tables) still extracts. — **Status**: Verified (RF-10) — **Method**: Spike.
- [x] A2 (reframed, not verified as stated): Restarting the MinerU server frees a slot held by a stuck page, so the
  next page is not queued behind it. — **Status**: Reframed by RF-12, measured on a
  three-slot server: a client
  timeout did not queue the next page, and a restart costs about 30 s, so the
  design restarts only after two consecutive timeouts (Technical Design, Bounds).
  A page that never finishes is the remaining case, and the restart is its
  release. — **Method**: Spike.
- [x] A3: MinerU's output on non-math, born-digital PDFs is at least as good for
  retrieval as Docling's. — **Status**: Verified by proxy (RF-11): text recall
  within two points; a retrieval evaluation was not run. — **Method**: Spike.
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

A page descends a rung on a timeout, on `MineruMemoryError`, and on any other
`RuntimeError` from the worker. `MineruMachineResourceError` (an `Exception`, not
a `RuntimeError`), `MineruWorkerStopped` and every other `BaseException`, a
cancelled run included, propagate and stop the document (RF-15).

**Systemic failure.** If three consecutive pages leave rung 1 for the same reason
(the same exception type and the same leading message line, a timeout counted as
one reason), the cause is the install or the machine, not the pages: the document
stops with that reason, and no further page is tried. The threshold is
`pdf.ladder_breaker` (default 3; 0 turns it off). A document whose every page is
`unextracted` fails with a named reason rather than indexing nothing.

Docling is removed: code, dependency, configuration value, CLI choice, doctor
row, CI steps, docs and tests.

### Technical Design

**Bounds.** Each rung has a time limit, and the subprocess path is the only one
that can actually stop work, so it is the path a timed-out page retries on:

- the server request timeout becomes `mineru_page_timeout_s` × pages in the
  batch (today a fixed 300 s, RF-3);
- a server timeout sends that page to the next rung on the subprocess path,
  never the same rung again on the server. The server is restarted only after two
  consecutive server timeouts, the sign of a page that will not finish: an
  abandoned request otherwise completes on its own and does not delay the next
  one, and a restart costs about 30 s of model reload (RF-12);
- the longest interval with no progress write is one rung attempt, plus a
  server restart when one lands inside it. Config load refuses
  `mineru_page_timeout_s` × `mineru_page_batch` + 30 s above 240 s (the
  300 s stale window less a 60 s margin). The defaults, 180 s × 1 + 30 s =
  210 s, pass, and RF-10's slowest cold page (129 s) fits under 180 s.

A page's worst case is rung 1 and rung 2 both timing out, about 360 s, plus about
30 s if a restart falls in it, plus rung 3's text read; a 300-page book whose every
page did that would take about 33 hours, which is why the systemic-failure rule
stops a document after three consecutive pages leave rung 1 for one reason.

**Progress.** Each rung attempt opens with a progress write that re-sends the
current `pages_extracted`, which stamps the heartbeat (RF-13), and each page
writes progress again when it settles. The longest silent interval is therefore
one rung attempt plus a restart, which config load keeps under 240 s (Bounds).
This closes nexus-lqaq1 for the remaining extractor without an engine heartbeat
route.

**What each document and page records.** `extraction_method` stays one
document-level value on every chunk (RF-14): `mineru` when every page settled on
rung 1, `mineru+degraded` when any page settled lower, `pymupdf` for the explicit
text-only extractor. The per-page detail is a document-level map,
`extraction_pages`, from rung (`mineru-no-formula`, `pymupdf-text`,
`unextracted`) to the 1-based page numbers that settled there; it rides the
extraction result's metadata into the pipeline row's `extraction_meta` for the
run, and the PDF catalog hook merges it into the catalog document's `meta`, where
it lasts. `nx store get` prints `Extractor:` as today and a degraded-pages line
from the catalog document. The run output names degraded pages. Existing values
(`docling`, `mineru+docling-degraded`, `pymupdf_normalized`) stay readable on
documents already indexed.

**Configuration and CLI.** `pdf.extractor` and `--extractor` accept `mineru`
(the default) and `pymupdf`. The values `auto` and `docling`, still present in
users' configs and scripts, mean `mineru` and print a one-line deprecation notice
once per process; the CLI choices keep accepting them, and an invalid config value
falls back to `mineru` (today it falls back to `auto`, `config.py:184-189`).
`--on-formula-oom` is retired, since the ladder handles that case.

**`pymupdf`, the explicit text-only extractor (Sam, 2026-10-27).** For a host that
cannot run MinerU, `--extractor pymupdf` (or `pdf.extractor: pymupdf`) runs only
rung 3 for every page: the PDF's own text layer, with no layout, tables or math.
Pages are stamped `pymupdf-text`, an image-only page is `unextracted`, and the run
names the extractor in its summary so the result is never mistaken for a MinerU
extraction. It needs only PyMuPDF, a core dependency, so it works without the
`pdf` extra: the six stack gates (`pdf_extractor.py:1510`, `doc_indexer.py:2742`,
`commands/index.py:3177` and `:3221`, `commands/dt.py:1098` and `:1990`) take the
extractor and neither require nor install the stack for `pymupdf`, and it skips
the MinerU server probe (`commands/index.py:3242`). It is never chosen
automatically.

**No MinerU at all.** With the default `mineru`, a missing MinerU fails the run
at once and names both fixes: install the PDF stack (`pdf_stack` already words
this) or rerun with `--extractor pymupdf` for text only. It does not degrade every
page to its text layer on its own.

**Title.** The resolution chain (`indexer_utils.py:644-675`) loses its first
step, `docling_title`, and keeps the rest (`pdf_title`, the first heading, then
the filename). MinerU and the PyMuPDF path already write an empty
`docling_title`, so only the key and its read go; documents that carry one keep it.

**Page count.** The document's page count and page boundaries come from the
PDF's own page total and the per-page outcomes, not from the length of MinerU's
`pdf_info` (`pdf_extractor.py:2957`), which a degraded page leaves short.

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
| Server timeout and recovery | `_mineru_run_via_server`, `_restart_mineru_server` | Extend: per-page timeout; a timed-out page leaves the server for the next rung and does not mark the server down; two consecutive pages whose rung-1 server attempt timed out trigger one restart through `_restart_mineru_server`, inside its existing budget (`_MINERU_MAX_RESTARTS`, 2 per extractor, `pdf_extractor.py:2442`) and machine-wide cooldown; with the budget spent, the server is marked down as today. A restart ends peers' in-flight requests on the shared server, which recover through their own connection-error path. |
| Missing-stack message | `pdf_stack.require` / `missing_message` | Reuse; drop Docling from `STACK_DISTS`/`STACK_MODULES`. |
| Doctor | `health._check_pdf_support`, `_check_docling_importable` | Retarget the Docling row to MinerU's pipeline modules (`mineru`, `cv2`, `torch`, `torchvision`), which it is the only check for (`health.py:2774-2776`); reword the MinerU server row, which says "math PDFs". |
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
- **Re-extract every Docling-extracted document under MinerU**: about 4 to 10 s
  per page on CPU (RF-1, RF-4, RF-11) across a whole library, unasked for; `nx index pdf --force` does it
  for any document a user chooses.

## Trade-offs

### Consequences

- Extraction is slower for non-math, born-digital PDFs: about 4 to 10 s per page
  on CPU across the measured PDFs (RF-1, RF-4, RF-11) against about 0.28 s for
  Docling's unenriched pass (RF-2). A 300-page book goes from about 1.5 minutes to
  between about 20 and 50, with steady progress instead of silence. Docling's
  enriched default, the path `--extractor docling` took, is not a fast baseline:
  it ran 2.6 hours on a 30-page PDF (RF-11). Sam accepted this cost.
- Math and scans extract better (RF-1), and no routing guess is made.
- A document always finishes; degraded pages are named and countable.
- The install loses Docling and its models; CI loses a priming action and a step.

### Risks and Mitigations

- **Risk**: MinerU's non-math output is worse than Docling's for some PDFs (A3,
  verified only by proxy in RF-11). **Mitigation**: the MVV documents; a retrieval
  regression found later is taken back to Sam.
- **Risk**: `formula_enable=false` behaves differently on another platform or
  MinerU release (A1, verified on chas with 3.1.11). **Mitigation**: the MinerU
  pin; the MVV runs on the laptop.
- **Risk**: users scripted `--extractor docling` or `--on-formula-oom docling`.
  **Mitigation**: `docling` maps to `mineru` with a notice; `--on-formula-oom`
  stays accepted and ignored for one release, with a notice.

### Failure Modes

- MinerU absent: the run fails at once with the install command (visible).
- A page settles on rung 3 or 4: the run output names it and the document
  summary counts it (visible); the page is searchable by its text layer or not at all.
- The MinerU server wedges: the page times out and moves to the subprocess path
  at the next rung; a second consecutive timeout restarts the server (logged with
  the page numbers).
- MinerU is installed but broken (a module that fails to import, missing weights):
  three consecutive pages fail rung 1 for that reason and the document stops with
  it, instead of indexing every page as text.

## Implementation Plan

### Prerequisites

- [x] A1 to A5 verified or reframed (RF-8 to RF-12).
- [x] Q1 answered by Sam (2026-10-27): add `--extractor pymupdf`.

### Minimum Viable Validation

On the laptop, with Docling uninstalled, index three PDFs end to end: the
Grossberg & Stone 1986 scan (RF-1), the 19-page paper that hung Docling for
9 h 40 min (nexus-73pvi), and the 300-page TCoDEVONthink manual (RF-2). Each
finishes within its page bound, keeps the equations RF-1 found, and names any
degraded page. Repeat one with a page forced to time out on rungs 1 and 2, to show
rung 3 settle it and the longest gap between progress writes stay under 240 s.

### Phase 1: Code Implementation

#### Step 1: The ladder

Per-page outcome type; rung 2 (MinerU without formulas) and rung 3 (one-page
PyMuPDF text); the descend-or-propagate rule and the systemic-failure rule; the
document-level `extraction_method` and the `extraction_pages` map in the result
metadata, merged into the catalog document's `meta` by the PDF hook; page count
from the page total; `nx store get`'s degraded-pages line. Tests first, with a fake
MinerU that fails on chosen pages and rungs.

#### Step 2: Bounds

Server timeout from `mineru_page_timeout_s`; a timed-out page goes to the next
rung on the subprocess path; the restart rule; a progress write at the start of
each rung attempt; config load refuses `mineru_page_timeout_s` ×
`mineru_page_batch` + 30 s above 240 s.

#### Step 3: Remove Docling

Router, Docling code paths, `pdf_stack` entries, constraints pins, the `pdf`
extra entry, `--on-formula-oom`; the doctor row retargeted; `auto` and `docling`
become aliases with a notice; the six stack gates made extractor-aware for
`pymupdf`; recovery and help text that points at Docling (`commands/index.py`,
`commands/dt.py`, the quality-gate remedy in `pdf_extractor.py`); title chain;
docs and size text. The surface, by `git grep -i docling` (RF-15): `.github`
(the `prime-docling` action, `ci.yml`, the nightly local-service gate,
`dependency-drift-watch.yml`, `prime-mineru` comments), `scripts/`
(`ci_warm_docling.py` and six others), 31 files under `src/`, nine docs outside
`docs/rdr`, three `conexus/` files, `mcpb/manifest.json`, three `web/` files,
`README.md`, `pyproject.toml`, and 68 test files. CI: unit tests that assert real
Docling extraction become fake-backed, and the Docling non-vacuity step is
replaced by one that extracts a one-page fixture through real MinerU, primed by
the existing `prime-mineru` action.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `extraction_pages` on the catalog document's `meta`, and the document-level `extraction_method` on chunks | In scope: `nx search --where extraction_method=mineru+degraded` lists chunks of degraded documents | In scope: `nx store get` and `nx catalog show` print the degraded pages | N/A: deleted with the document | In scope: a re-extract (`nx index pdf --force`) rewrites both | N/A: rebuilt by re-extraction |
| Documents extracted before this RDR (`docling`, `mineru+docling-degraded`) | In scope: `--where extraction_method=docling` or `=mineru+docling-degraded` | N/A | N/A | N/A | N/A; `nx index pdf --force` re-extracts any the user chooses |

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
- **Scenario**: a server request times out — **Verify**: the page retries on the
  subprocess path at the next rung, not on the server, and the server is not
  restarted.
- **Scenario**: two consecutive server requests time out — **Verify**: the server
  is restarted once and later pages use it again.
- **Scenario**: MinerU not installed — **Verify**: immediate failure naming both
  fixes (install the PDF stack, or `--extractor pymupdf`), no page extracted.
- **Scenario**: `--extractor pymupdf` with the PDF stack genuinely absent —
  **Verify**: no stack install is attempted, every page with a text layer is
  `pymupdf-text`, image-only pages are `unextracted`, MinerU is never started or
  probed, and the summary names the extractor.
- **Scenario**: `pdf.extractor: docling` in config — **Verify**: runs MinerU, one notice.
- **Scenario**: `mineru_page_timeout_s` × `mineru_page_batch` + 30 s above 240 s —
  **Verify**: refused at config load; the defaults pass.
- **Scenario**: a page times out on rungs 1 and 2 — **Verify**: it settles
  `pymupdf-text`, and the longest gap between progress writes stays under 240 s.
- **Scenario**: three consecutive pages fail rung 1 with the same import error —
  **Verify**: the document stops with that reason; no later page is tried.
- **Scenario**: every page is `unextracted` — **Verify**: the document fails with a
  named reason.
- **Scenario**: the run is cancelled while a page is on rung 2 — **Verify**: the
  cancel propagates; no page is degraded by it.
- **Scenario**: a degraded middle page — **Verify**: the page count and the page
  numbers of later pages are right.
- **Scenario**: a document with degraded pages — **Verify**: chunks carry
  `mineru+degraded`, the catalog document's `meta` carries `extraction_pages`, and
  `nx store get` prints the pages.

## Validation

### Testing Strategy

Storage-free unit tests drive the ladder with a fake MinerU. The engine-backed
pipeline tests (`test_rdr223_pdf_journey` and peers) prove progress and the
takeover window. The MVV runs real MinerU on the three named PDFs.

### Performance Expectations

The per-page costs are RF-1, RF-2 and RF-4; this RDR measures the three MVV
documents on the laptop and chas before the RDR closes.

## Finalization Gate

### Contradiction Check

One contradiction was found and fixed before the gate: Consequences and Briefly
Rejected stated MinerU's cost as "about 6 to 7 s per page" from RF-1 and RF-4,
while RF-11 measured about 10 s per page on `tc-sql.pdf` and about 4 on
`2408.04948.pdf`; both now cite the 4 to 10 s range of all three findings.

RF-3 describes today's server recovery (restart, retry the same batch on the
server, then the subprocess path); the Technical Design replaces it, and RF-12
is the reason the restart now waits for a second consecutive timeout. Gap 5's
13-minute worst case comes from that retry logic in nexus, not from the server
queueing work, which RF-12 did not observe; the two do not conflict.

Gate round 1 found two more: the Progress paragraph claimed a bound the default
limits did not give, and per-page `extraction_method` had no valid storage. Both
are fixed in the Technical Design (Bounds, Progress, What each document and page
records).

### Assumption Verification

All five Critical Assumptions are resolved:

- A1 (formula switch per call): Verified by spike, RF-10.
- A2 (restart frees a stuck slot): reframed by spike, RF-12. A client timeout did
  not queue the next page, so the design restarts only after two consecutive
  timeouts. A page that never finishes was not reproduced; the restart is its
  release, and the Test Plan's two-consecutive-timeouts scenario drives it with a
  fake.
- A3 (MinerU's non-math quality): Verified by proxy, RF-11, text recall within two
  points of Docling. A retrieval evaluation was not run; the MVV's three documents
  and the existing RF-1 retrieval result (MRR 0.74 against 0.65 on a scanned math
  paper) are the remaining evidence. This is an accepted residual risk.
- A4 (one page's text layer): Verified by source search, RF-8.
- A5 (no other importer of Docling-only packages): Verified by source search, RF-9.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `do_parse(..., formula_enable=...)` | MinerU 3.1.11 | Spike (RF-10) |
| `POST /file_parse` `formula_enable` | MinerU 3.1.11 | Spike (RF-10) |
| `page.get_text()` | PyMuPDF | Source Search (RF-8) |

### Scope Verification

The MVV is in scope and runs before the RDR closes.

### Cross-Cutting Concerns

- **Versioning**: `auto`/`docling` aliases and a retired `--on-formula-oom`, each
  with a notice for one release.
- **Build tool compatibility**: N/A.
- **Licensing**: removing a dependency only.
- **Deployment model**: client change; no engine change.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Docling-extracted documents stay; re-extraction is opt-in. A host without MinerU indexes text only with `--extractor pymupdf`.
- **Secret/credential lifecycle**: N/A.
- **Memory management**: MinerU server memory per RF-4; Docling's in-process
  converter (about 2 GB) leaves the indexing process.

### Proportionality

Right-sized for a change that removes a dependency across code, CI and docs and
replaces a routing guess with a per-page ladder. The Research Findings carry the
evidence Sam asked for (the measured costs and the A1 to A5 spikes); no section is
a candidate for trimming.

## Open Questions for Sam

- **Q1** (answered 2026-10-27): should `--extractor pymupdf` (text layer only, no
  layout, tables or math) exist as an explicit choice for a host that cannot run
  MinerU? Yes; see Technical Design, "`pymupdf`, the explicit text-only extractor".

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
- 2026-10-27: research RF-8 to RF-12; A2 reframed, so a server timeout no longer restarts the server unless it repeats.
- 2026-10-27: Q1 answered by Sam: `--extractor pymupdf` added as an explicit text-only choice.
- 2026-10-27: Gate round 1 — BLOCKED (2 Critical, 6 Significant, 2 ship-blocker(s)); commit `ddacb282b`; critique `nexus_rdr/237-gate-critique-2026-10-27-r1`.
