# RDR-237 Post-Mortem: MinerU-Only PDF Extraction with a Per-Page Fallback Ladder

**Closed** 2026-10-30 · **Accepted** 2026-10-27 · **Epic** nexus-tr1rj (all children and the residual beads nexus-q6nw0, nexus-489n1, nexus-tv1w3 closed) · **Shipped** conexus 7.89.0 (Phase 1: the ladder, the bounds, Docling removed); the follow-ups after it (failure-class keys, the server slot count, the doctor's PDF settings row, re-measured install sizes) are on develop for the next release

## What the RDR set out to do

Docling was the default PDF extractor, and Sam named three failures in it:
extraction could block for hours with nothing reported (one 19-page paper ran
9 h 40 min), it parsed math and scanned papers badly, and a failure stopped the
document with no fallback. The `auto` route also sent every scanned math paper
to Docling, because it judged "math" from the scan's old OCR text layer.

The RDR made MinerU the only extractor and gave each page a fallback ladder:
MinerU with formulas, MinerU without formulas, the page's own text layer, and
finally `unextracted`. Every attempt is bounded by a per-page timeout so the
pipeline heartbeat never goes stale, a breaker stops a document only when
consecutive pages fail the same way (the install or the machine is broken),
`--extractor pymupdf` is an explicit text-only choice, and Docling is removed
with its models, pins, CI steps and tests.

## Implementation status

Implemented. Phase 1 shipped in 7.89.0. The decisions made after acceptance are
in the RDR's Revision History and Key Discoveries RF-16 to RF-19; the RDR's
Systemic failure paragraph was amended on 2026-10-29 to name the failure classes
as built. The Minimum Viable Validation (RF-17) ran on develop `7e749db83`; the
later failure-class, slot-count and bounds changes are covered by unit and
real-worker tests and the release's post-publish formula-PDF check, not by a
second MVV run.

---

## Implementation vs. plan

### What was implemented as planned

- The four-rung ladder, per page, with a page descending on a timeout, a
  `MineruMemoryError`, any other worker `RuntimeError`, or a dropped server
  connection.
- The breaker: a page counts only when rungs 1 and 2 fail with the same class,
  and `pdf.ladder_breaker` consecutive counted pages fail the document.
- The bounds: the server path uses the configured per-page timeout instead of a
  fixed 300 s, and a stuck page falls to the killable subprocess path.
- Docling removed from the code, the dependency pins, CI, the doctor and the
  tests; `auto` and `docling` run as `mineru` with a one-line notice.
- `--extractor pymupdf` as a text-only extractor recorded as `pymupdf`.

### What diverged from the plan

- **Breaker trip keeps earlier pages**: the RDR had a trip fail the whole
  document. The code keeps the pages before the failing window in the pipeline
  buffer and drops the window. Sam amended the RDR to match on 2026-10-27.
- **Failure-class keys**: the RDR keyed classes as `timeout`, `exit:<rc>` plus
  the stderr line, `oom`, and `http:<status>`. `http:<status>` was never
  reachable, since a server error falls to the subprocess path. A worker the OS
  killed ends its stderr in a progress-bar frame that differs page to page, so
  keying on that line gave two identical kills two classes and the breaker never
  tripped. The built keys are `timeout`, `signal:<n>`, `oom`,
  `oom:<rc>:<line>`, `exit:<rc>:<line>` with the line normalised, and
  `unclassified` (RF-16, RF-19).
- **Server slot count**: the restart rule guessed the MinerU server's
  concurrency from the platform (one slot on macOS, three elsewhere). The code
  reads `max_concurrent_requests` from the server's `/health` and keeps the
  guess only as a fallback (RF-18).
- **A trip across many PDFs**: the RDR said what a trip does to one document.
  Under `nx index pdf --dir` a trip now stops the run and lists the rest as not
  attempted; in `nx dt index` and `nx index repo` a trip or a machine-resource
  error skips and lists the remaining PDFs and keeps other work (Sam's decision,
  then decisions Sam delegated, 2026-10-27 and 2026-10-28).
- **Rerun with pymupdf over a MinerU buffer**: not covered by the RDR. It starts
  clean and extracts every page from the text layer, so a document is never a
  mixture recorded as `pymupdf` (delegated by Sam, 2026-10-28).
- **A document with no extracted page**: the RDR recorded such pages as
  `unextracted` but said nothing about the document. It raises
  `AllPagesUnextractedError`, which fails `nx index pdf` and `nx dt index`, and
  stays a per-file skip under its own error class in `nx index repo` (Sam,
  2026-10-29).
- **Output defects stop the document**: the Approach listed what propagates
  past the ladder, but two MinerU output defects also stop the document: more
  `pdf_info` entries than pages asked for (a failed page rewrite that would
  duplicate every page) and a `content_list` or `middle` file that is not valid
  JSON. The close-time critique found the gap; RF-20 records it.

### What was added beyond the plan

- A `PDF settings` row in `nx doctor`, and the same red result from
  `nx doctor --check-mineru`, for a `[pdf]` setting that holds no integer or a
  page timeout times page batch above the 240 s bound.
- `--on-formula-oom` kept as an accepted and ignored option for one release, so
  old scripts do not break.
- The release's MinerU end-to-end check is the post-publish
  `fresh-install-mvv.sh --published` formula-PDF run.

### What was planned but not implemented

- Nothing from the Problem Statement's six gaps. The install sizes in the README,
  docs, setup tool and `.mcpb` manifest shipped in 7.89.0 still measured with
  Docling in the stack; they were re-measured on 7.89.0 afterwards and corrected
  on develop (nexus-tr1rj.18).

---

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 2 | `http:<status>` reachable; platform guess of server slots | Yes, source search of the MinerU server (`fast_api.py`) |
| **Framework API detail** | 1 | killed worker's stderr ends in a progress-bar frame | Yes, spike: kill a worker and read its stderr |
| **Missing failure mode** | 4 | trip across a multi-PDF run; document with no extracted page; trip discarding good pages; output defects that stop the document | Partly, by walking each caller of the extractor |
| **Missing Day 2 operation** | 2 | pymupdf rerun over a MinerU buffer; install sizes not re-measured before release | Yes, the release checklist names measured figures |
| **Deferred critical constraint** | 0 | | |
| **Over-specified code** | 0 | | |
| **Under-specified architecture** | 0 | | |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 0 | | |
| **Missing cross-cutting concern** | 0 | | |

### Pattern references

- Missing failure mode (4): three instances sat at a caller, not in the extractor,
  and the fourth was a pre-existing stop the Approach did not list.
  The RDR designed one document's ladder carefully and left what `nx index pdf
  --dir`, `nx dt index` and `nx index repo` do with its outcomes to later
  decisions. Next time, list every caller of the changed function in the
  Technical Design and say what each does with each new outcome.
- Unvalidated assumption (2) and the framework detail: all three came from
  writing down MinerU's behaviour from its documentation and our old code instead
  of reading the server source or making a worker fail. A short spike that kills
  a worker and hits `/health` would have caught them before the gate.
