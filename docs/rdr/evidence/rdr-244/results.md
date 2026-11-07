# RDR-244 Track A results

The pre-registered run (`preregistration.md`, committed at cc378a4a6 before any run) of `nx search` over the CoIR subset. The decision taken from it is "baseline only" (Sam, 2026-11-07): these numbers are a reproducible baseline, published as they fell, with no product decision attached and no parity or gap claim against CoIR.

## Numbers

Per dataset; each value has a 95% percentile bootstrap interval over the 50 sampled queries (10,000 resamples, seed 2026, nearest-rank endpoints). The interval covers the choice of queries only, not the corpus draw. Every pool dataset is Apache-2.0, by the CoIR paper's licence table and the upstream sources; the Hugging Face cards state no licence (T2 `nexus_rdr/244-research-15`; Sam, 2026-11-06).

| Dataset | Corpus loaded | NDCG@10 | Recall@10 | MRR | Latency p50 / p95 |
|---|---|---|---|---|---|
| codetrans-dl | 816 of 816 (whole) | 0.503 [0.416, 0.593] | 0.84 [0.74, 0.94] | 0.398 [0.307, 0.496] | 1.58 s [1.52, 1.68] / 1.96 s [1.87, 2.15] |
| codetrans-contest | 1,008 of 1,008 (whole) | 0.517 [0.401, 0.632] | 0.68 [0.54, 0.80] | 0.467 [0.348, 0.587] | 1.45 s [1.34, 1.55] / 1.86 s [1.72, 2.15] |
| synthetic-text2sql | 5,000 of 105,851 (4.7%) | 0.935 [0.874, 0.986] | 0.98 [0.94, 1.00] | 0.922 [0.850, 0.982] | 1.16 s [1.15, 1.17] / 1.25 s [1.22, 1.30] |
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

These are not comparable numbers, and no claim is made from the difference. The first three caveats below raise this run's numbers relative to CoIR's; the last two have no stated direction:

- **Sampled corpus (easier).** synthetic-text2sql loaded 4.7% of its corpus: every relevant document and a seeded sample of the rest, so retrieval is easier than over CoIR's full corpus. The two codetrans datasets loaded whole.
- **Identical-text merge (higher).** Byte-identical documents count as one (Sam, 2026-11-06). In codetrans-dl all 816 documents fall into 266 classes (550 absorbed), and the relevant document of each of the 50 sampled queries has a twin, which CoIR scores as a miss. In the synthetic-text2sql sample 36 documents fall into 18 classes (18 absorbed), and 1 of the 50 sampled queries' relevant documents has a twin. codetrans-contest has none.
- **Shared-chunk credit (higher).** A row whose chunk belongs to several classes credits each unseen class at consecutive ranks, which is generous to the system. 22 such rows fell inside a top ten in codetrans-dl, none in the other two.
- **Chunk-level retrieval (no stated direction).** `nx search` retrieves chunks and the scorer deduplicates them to documents; CoIR embeds each document whole.
- **Warm client-side latency (not compared).** Wall-clock time of the `nx search` subprocess after one warm-up query per collection, including the command's start-up.

## Run record

- Engine: self-provisioned loopback engine, release 0.1.181, `embedding_mode` onnx-local, `embedding_profile` bge; conexus 7.97.0 at cc378a4a6, clean tree. Host chas, `lab-run -c lab-m`. Every pin is in `step2/results.json`; each dataset's sample manifest and text hashes sit beside it.
- Attempt 1 (`step2-attempt1/`) stopped partway: the client session holding the run dropped after about 60 minutes and took the run with it, partway through codefeedback-mt's ingest. Sam: "Yes, rerun unchanged" (2026-11-07). Attempt 2 (`step2/`) is the published run. Its codetrans-dl and codetrans-contest per-query rankings, scores, intervals, manifest hashes and pins are identical to attempt 1's.
- The two codefeedback datasets were dropped by the pre-registered cap, not substituted: neither dataset's 5,000-document sample finished ingesting within 1,800 s (how many had loaded at the cut is not recorded). The run's own ingest rates were 0.22 s per document (codetrans-dl), 0.58 s (codetrans-contest) and 0.07 s (synthetic-text2sql). A run with the codefeedback datasets is a different run and would be labelled as one.
- Refusals that did not fire: no rerank, bibliographic or boost signal on any row; 0 taxonomy assignments on bench chunks; no excluded documents or queries; no query with fewer than ten distinct documents.

## RDR-090, beside it

RDR-090's internal bench compared three routes on five queries over the nexus RDR corpus, NDCG@3 (`scripts/spikes/spike_rdr090_5q_summary.json`, unedited): path A, plain `nx search`, 0.555 at 9.0 s mean latency; path B, nx_answer under a fixed plan, 0.277 at 48.5 s; path C, nx_answer planning each query, 0.277 at 36.8 s. Its pre-registered criterion, a path difference of at least 0.05 in NDCG@3 on at least 3 of 5 queries, failed: two of five passed (Q3 and Q4, where A scored 0.917 and 0.469 and B and C scored 0). The recorded causes (T2 `nexus_rdr/090-research-2`) are part of the result: plan leakage, plan-library pollution, Q2's labels using words the indexed text does not (it scored 0 on every path), and the Chroma-era substrate it ran on. The N=30 run it planned never ran.
