# RDR-244 Track A results

The pre-registered run (`preregistration.md`, committed at cc378a4a6 before any run) of `nx search` over the CoIR subset. The decision taken from it is "baseline only" (Sam, 2026-11-07): these numbers are a reproducible baseline, published as they fell, with no product decision attached and no parity or gap claim against CoIR.

## Numbers

Per dataset; each value has a 95% percentile bootstrap interval over the 50 sampled queries (10,000 resamples, seed 2026). The interval covers the choice of queries only, not the corpus draw.

| Dataset | Corpus loaded | NDCG@10 | Recall@10 | MRR | Latency p50 / p95 |
|---|---|---|---|---|---|
| codetrans-dl | 816 of 816 (whole) | 0.503 [0.417, 0.593] | 0.84 [0.74, 0.94] | 0.398 [0.307, 0.496] | 1.58 s / 1.96 s |
| codetrans-contest | 1,008 of 1,008 (whole) | 0.517 [0.401, 0.632] | 0.68 [0.54, 0.80] | 0.467 [0.348, 0.587] | 1.45 s / 1.86 s |
| synthetic-text2sql | 5,000 of 105,851 (4.7%) | 0.935 [0.874, 0.986] | 0.98 [0.94, 1.00] | 0.922 [0.850, 0.982] | 1.16 s / 1.25 s |
| codefeedback-st | dropped: ingest passed the 30-minute cap | | | | |
| codefeedback-mt | dropped: ingest passed the 30-minute cap | | | | |

Latency pooled over the three scored datasets (secondary): p50 1.36 s, p95 1.92 s, 150 queries.

## Beside CoIR's published figures

CoIR (arxiv.org/abs/2407.02883) Table 3 reports NDCG@10 on a 0 to 100 scale; its BGE-Base row is bge-base-en-v1.5 (Table 10), the model this run embeds with. Shown on the same scale:

| Dataset | This run, NDCG@10 | CoIR BGE-Base | CoIR best in Table 3 |
|---|---|---|---|
| codetrans-dl | 50.3 | 21.71 | 33.24 (E5-Mistral) |
| codetrans-contest | 51.7 | 38.50 | 82.55 (E5-Mistral) |
| synthetic-text2sql | 93.5 | 45.59 | 69.26 (Voyage-Code-002) |

These are not comparable numbers, and no claim is made from the difference. Each caveat below moves this run's numbers in a known direction:

- **Sampled corpus (easier).** synthetic-text2sql loaded 4.7% of its corpus: every relevant document and a seeded sample of the rest, so retrieval is easier than over CoIR's full corpus. The two codetrans datasets loaded whole.
- **Identical-text merge (higher).** Byte-identical documents count as one (Sam, 2026-11-06). In codetrans-dl 550 documents merged into 266 classes, and every test query's relevant document has a twin, which CoIR scores as a miss. In the synthetic-text2sql sample 18 documents merged. codetrans-contest has none.
- **Chunk-level retrieval.** `nx search` retrieves chunks and the scorer deduplicates them to documents; CoIR embeds each document whole.
- **Warm client-side latency.** Wall-clock time of the `nx search` subprocess after one warm-up query per collection, including the command's start-up.

## Run record

- Engine: self-provisioned loopback engine, release 0.1.181, `embedding_mode` onnx-local, `embedding_profile` bge; conexus 7.97.0 at cc378a4a6, clean tree. Host chas, `lab-run -c lab-m`. Every pin is in `step2/results.json`; each dataset's sample manifest and text hashes sit beside it.
- Attempt 1 (`step2-attempt1/`) stopped partway: the client session holding the run dropped after about 60 minutes and took the run with it, partway through codefeedback-mt's ingest. Sam: "Yes, rerun unchanged" (2026-11-07). Attempt 2 (`step2/`) is the published run. Its codetrans-dl and codetrans-contest per-query rankings, scores, intervals, manifest hashes and pins are identical to attempt 1's.
- The two codefeedback datasets were dropped by the pre-registered cap, not substituted. Ingest on chas lab-m ran about 0.58 s per document for codetrans-contest (589 s for 1,008), against Step 0's 0.03 to 0.18 s on the laptop, so 5,000 codefeedback documents did not fit in 30 minutes. A run with them is a different run and would be labelled as one.
- Refusals that did not fire: no rerank, bibliographic or boost signal on any row; 0 taxonomy assignments on bench chunks; no excluded documents or queries; no query with fewer than ten distinct documents.
