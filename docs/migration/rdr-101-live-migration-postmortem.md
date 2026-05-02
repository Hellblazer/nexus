# RDR-101 live-migration post-mortem (Hal's first run, 2026-05-01)

This is a forensic record of the first real-world run of the RDR-101
event-sourced catalog migration against Hal's production state. The
sandbox harness (`.9.10`/`.9.11`) and the migrate verb (`.9.9`) were
validated against synthetic corpora before this run; this is the
write-up of what synthetic harnesses missed.

The intent is two-fold: (1) close the loop on the defects this run
surfaced (`.9.14`, `.9.15`, `.9.18`, `.9.19`) so future operators don't
re-discover them, and (2) record the methodology — sandbox restore +
real-data dry-run — for the next migration that lands.

## Catalog under test

| | Value |
| --- | --- |
| Catalog tarball | `nexus-catalog-backup-20260501-165411.tar.gz` (2.7 GB) |
| Documents | 14,817 |
| Events (post-synthesize) | 348,958 |
| T3 collections | 142 |
| T3 export size | 1.2 GB across 142 `.nxexp` files |
| T3 chunks | 312,514 |
| Cloud T3 latency | ~50–100 ms / `col.update` round-trip |

## Defects surfaced

Each one was either invisible to the sandbox harness or visible only
under real-data shape. Filed beads, fix PRs, and root-cause notes:

### `.9.14` — migrate must rebuild the live SQLite projection

* **Symptom:** `nx catalog migrate` returned exit 0 against the live
  catalog, but `nx catalog doctor --replay-equality` then reported
  the SQLite projection diverged from `events.jsonl`.
* **Root cause:** `migrate` sequenced
  `synthesize-log --force + t3-backfill-doc-id + doctor` but did NOT
  rebuild the projection database. After synthesize rewrote
  `events.jsonl`, the on-disk SQLite was stale.
* **Why the harness missed it:** the synthetic corpora started from
  empty SQLite, so projection rebuild was implicit on first read.
  Hal's catalog had a pre-existing projection from prior verb runs.
* **Fix:** add an explicit projection rebuild step inside `migrate`,
  bracketed between synthesize-log and doctor. Idempotent — no-op if
  projection is already coherent.

### `.9.15` — `store import` of vector-only collection raised NoneType

* **Symptom:** `nx store import` of a `.nxexp` whose payload had no
  `documents` field (vector-only collection — embeddings + metadata,
  no source text) crashed in the import path with `'NoneType' object
  has no attribute '__len__'`.
* **Root cause:** the import path assumed every export carries the
  source documents, but several of Hal's collections were vector-only
  (embeddings indexed at write time, source content held elsewhere).
* **Why the harness missed it:** synthetic exports always include the
  documents field, so the None branch was untested.
* **Fix:** explicit None-guard on the documents payload; vector-only
  imports skip the documents-bearing add and use `add_with_embeddings`.

### `.9.18` — batch-level all-or-nothing rejection wasted clean chunks

* **Symptom:** `t3-backfill-doc-id` reported ~31k chunks in `errors`
  out of ~140k expected updates. Chunks reported as failed were not
  individually broken — they were swept along with one over-cap chunk
  per batch.
* **Root cause:** ChromaDB Cloud's `col.update` rejects the entire
  batch if any chunk in it violates the `NumMetadataKeys` quota
  (>32 keys). Hal's catalog has ~9k chunks at 35–36 keys (legacy
  documents pre-Phase-4 prune-deprecated-keys verb), distributed
  uniformly across collections. Each batch of 300 had ~50% chance of
  including at least one over-cap chunk → ~50% of batches rejected →
  ~70k chunks falsely reported failed.
* **Why the harness missed it:** the synthetic harness used clean
  metadata only; we never hit the live-data shape where over-cap
  chunks coexist with under-cap chunks in the same collection.
* **Fix:** per-chunk retry on batch failure. After an exception on
  the batch, re-issue `col.update` once per chunk so clean chunks
  land cleanly; over-cap chunks fail individually with their actual
  error message and land in `chunks_deferred` (not `errors`) when
  the error string carries the `NumMetadataKeys` hint. Verb exits 0
  if the only failures are deferred-class.

### `.9.19` — per-chunk retry was O(N), unacceptable on Cloud

* **Symptom:** the `.9.18` rerun against Hal's catalog ran for 30+
  minutes on the per-chunk retry phase before being cancelled.
* **Root cause:** 105 batches failed; each retry path did 300
  individual `col.update` round-trips at ~50–100 ms apiece →
  31,500 round-trips × 75 ms = ~40 minutes. Most of the work was
  wasted: only ~9k of the 31,500 chunks were genuinely over-cap;
  the rest were clean.
* **Fix:** recurse-and-halve. `_bisect_update` tries the whole slice
  first; on failure halves it, retries each half, and recurses to a
  single-chunk leaf. Worst case (1 over-cap chunk per batch of N):
  `2*log2(N) + 1` calls instead of `N + 1` — for N=300, that's ~18
  calls instead of 301 (~30× speedup). Best case (over-cap chunks
  clustered): even fewer because halves succeed wholesale.

## Defects NOT surfaced

For posterity, what worked end-to-end without modification:

* **Bootstrap guardrail.** The 95%-coverage guard correctly fell back
  to legacy reads on the first verb call before synthesize-log ran.
* **Synthesize-log.** Minted 14,817 `DocumentRegistered` events and
  312,514 `ChunkIndexed` events without errors; events.jsonl is
  byte-stable across reruns.
* **Doctor `--replay-equality`.** Compared the SQLite projection to
  the events.jsonl replay byte-for-byte and exited clean post-fix.
* **Doctor `--t3-doc-id-coverage`.** Counted the real number of T3
  chunks missing `doc_id` correctly across all 142 collections.

The event-sourcing core was correct. Every defect was at the I/O
boundary — exact-shape mismatches between synthetic and live data.

## Methodology — what worked, what to keep

The sandbox harness `.9.10`/`.9.11` proved its value under real data:

1. **Real catalog, isolated environment.** The full-stack sandbox script
   (`scripts/nexus-fullstack-sandbox.sh` — see `/tmp/nexus-fullstack-sandbox.sh`
   for the canonical version) restores the actual catalog tarball into a
   `/tmp` sandbox with isolated `NEXUS_CONFIG_DIR` and `NX_LOCAL_CHROMA_PATH`.
   No production state is at risk.
2. **Read-only against live state.** Only the tarball + `.nxexp`
   exports are read; the sandbox writes nothing back. Safe to rerun
   any number of times.
3. **End-to-end pipeline.** Imports every `.nxexp` into a local Chroma,
   runs pre- and post-migration doctors with the same flags an
   operator would use, exits non-zero if any stage didn't complete
   cleanly. Catches `.9.14`/`.9.15`/`.9.18` style defects before they
   touch production.

The lesson: **synthetic harnesses are necessary but not sufficient**.
For migration-class verbs, run a sandbox dry-run against real data
before declaring done. The .9.10/.9.11 sandbox is the template.

## Operator outcome

Post-`.9.18`, post-`.9.19`, post-projection-rebuild, the live migration
proceeds cleanly:

* `events.jsonl`: 348,958 events, byte-stable across reruns.
* T3 chunks: ~140k cleanly updated (full coverage less the deferred
  over-cap surface).
* `chunks_deferred`: ~9,084 over-cap chunks awaiting Phase 4
  `prune-deprecated-keys` (drops `source_path`, `title`, `git_branch`,
  `git_commit_hash`, `git_project_name`, `git_remote_url` once the
  reader migration is complete). At that point the deferred chunks
  drop under 32 keys and a single `t3-backfill-doc-id --resume` clears
  the residual.
* Doctor `--replay-equality`, `--t3-doc-id-coverage`, `--strict-not-in-t3`:
  all clean.

Migration verb behaviour for the operator: `nx catalog migrate` exits 0
once the deferred-cleanup population is the only remaining surface, and
prints an informational summary of the deferred class (count, expected
remediation, no action required pre-Phase-4).

## Phase 4 link

The deferred chunks block on `nexus-o6aa.10` (Phase 4 reader migration
+ `prune-deprecated-keys` verb). Until Phase 4 lands, the deferred
list is the operator's tracking surface; once it lands, a single
backfill rerun clears the residual and the catalog reaches full
RDR-101 steady state.

## Beads

| Bead | Status | Description |
| --- | --- | --- |
| `nexus-o6aa.9.14` | merged | migrate must rebuild SQLite projection |
| `nexus-o6aa.9.15` | merged | `store import` NoneType on vector-only collections |
| `nexus-o6aa.9.17` | open (#460) | progress output for migrate / synthesize-log / t3-backfill |
| `nexus-o6aa.9.18` | open (#459) | per-chunk retry on batch failure + deferred-class differentiation |
| `nexus-o6aa.9.19` | open (#461) | batch-bisect O(log N) recovery |
