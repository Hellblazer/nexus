# RDR-244 Track A pre-registration

Committed before the first run (RDR-244 Validation, Testing Strategy) and not changed after it. A run under any other value here is a different run and is labelled as one. Harness: `scripts/bench/coir_run.py`, `coir_sample.py`, `coir_score.py`.

## Subset

The five CoIR datasets the CoIR paper's licence table lists as Apache-2.0. The basis is that table and the upstream sources, which state the same licence; the Hugging Face cards state no licence at all (T2 `nexus_rdr/244-research-15`; Sam, 2026-11-06). `results.json` carries this as `licence_source`. Hugging Face revisions (`CoIR-Retrieval/<name>-queries-corpus` and `<name>-qrels`) are the ones Step 0 read (T2 `nexus_rdr/244-research-15`):

| Dataset | Collection subject | Corpus | Test queries | queries-corpus rev | qrels rev |
|---|---|---|---|---|---|
| codetrans-dl | deep-learning-code-translation | 816 | 180 | 328b6b68 | 3c26def7 |
| codetrans-contest | contest-code-translation | 1,008 | 221 | 60a64fc8 | 981ff059 |
| codefeedback-st | single-turn-code-feedback | 156,526 | 31,306 | 9ea30d04 | a5f9ffe7 |
| codefeedback-mt | multi-turn-code-feedback | 66,383 | 13,277 | ef78e2c8 | a81bd47c |
| synthetic-text2sql | text-to-sql | 105,851 | 5,851 | 9a918a5c | 14c01767 |

## Sample

- Queries: up to 50 per dataset, `random.Random(2026)` over the sorted test-split qrels query ids.
- Corpus: every document relevant to a kept query, then a `random.Random(2026)` fill from the remaining loadable documents (sorted by id), up to 5,000 documents per dataset (Sam, 2026-11-06). A smaller corpus loads whole. Only codetrans-dl and codetrans-contest load their whole corpus; codefeedback-st keeps about 3%, codefeedback-mt about 8%, synthetic-text2sql about 5%. Because every relevant document is kept and only non-relevant ones are dropped, a sampled corpus can only make retrieval easier than CoIR's full one. `results.json` gives `corpus_total`, `corpus_loaded` and `corpus_fraction` per dataset.
- A document is unloadable when its text is empty or over 16,384 UTF-8 bytes. It is excluded, and so is any sampled query whose relevant document it is; excluded queries are not replaced. Both are counted in `results.json`.
- Only positive judgements are kept; every qrels score in the pool is 1, and a dataset whose kept grades are anything else is dropped and named before its ingest.
- Each dataset's sample manifest (ids, judgements, exclusions, duplicate classes, and the SHA-256 of every loaded document's and kept query's text) is written beside `results.json` as `<dataset>.manifest.json`, and its SHA-256 is `query_set_sha256`. A changed text changes the hash even when the ids do not.

## Run

- One collection per dataset, in a minted tenant on a self-provisioned loopback engine, local mode (`bge-base-en-v1.5`, 768 dimensions; $0). The harness refuses to start when `NX_VOYAGE_API_KEY` is set, when the checkout has uncommitted changes, when a collection name lacks `bge-base-en-v15-768`, when the engine's `GET /version` reports another embedder, or when it reports a release below 0.1.175 or none (RDR-244 Technical Environment). It records that response in `results.json`. It refuses a collection that already holds content.
- Loader: `put_note`, `fire_note_chains` with an empty `HookRegistry`, `stamp_note`; title `<dataset>:<coir-id>`.
- Query: `nx search --corpus <collection> -m 100 --json -- "<query text>"`, no other flags; the query text is passed whole, with no length check; how the embedder treats a query past its token window is not measured, and a failed search stops the run. `search.hybrid_default` must be false, or the run refuses. No distance threshold applies: a bge collection gets none (`search_engine._threshold_for_collection_model`).
- Retrieval is over chunks: `put_note` splits a long document, and results are deduplicated to documents after retrieval. CoIR embeds each document whole, truncated once.
- A search that exits non-zero, prints one of nx's own `Warning:` or `note:` lines, returns no rows, runs past 300 s, or returns a row carrying a rerank, bibliographic or boost signal stops the run, as does any taxonomy assignment on a bench chunk.
- Run-time cap: 30 minutes of wall-clock time on each dataset's ingest (Sam, 2026-11-03, nexus-2k66t.2.7: "cap as proposed"). A dataset over the cap is dropped and named in `results.json` (`dropped`); nothing is substituted. Step 0 measured 0.03 to 0.18 s per document, about 15 minutes at most for 5,000.

## Scoring

- Rank order is `nx search`'s output order. Within a run of consecutive rows with equal distance, rows are ordered by their sorted document keys.
- A row's documents come from its chunk hash (`docs_for_chashes`), limited to the run's collection. A row with no document fails the run and names it.
- Identical-text documents count as one document (Sam, 2026-11-06): each class of byte-identical loaded documents is named by its smallest id, ranked once and judged with the highest grade among its members. Under CoIR's own scoring a retrieved twin of the relevant document is a miss, so the merge raises scores relative to CoIR wherever twins exist (every codetrans-dl test query and 5,517 of 5,851 synthetic-text2sql ones have one, T2 `nexus_rdr/244-research-16`). `results.json` reports the number of classes and of documents merged per dataset.
- A row holding several classes credits each unseen class in key order at consecutive ranks. Rows that credited more than one class inside the top ten are counted (`shared_rows`).
- K = 10 distinct classes. A query with fewer is scored on what came back, and the shortfall is counted.
- NDCG@10 with gain 2^g − 1 (every qrels score in the pool is 1, so this equals linear gain); document-level recall@10 (1 when any relevant class is in the top ten); MRR (1/rank of the first relevant class within ten, else 0).
- Latency: wall-clock time of each `nx search` subprocess, after one discarded warm-up query per dataset collection (a test query outside the sample); p50 and p95 by nearest rank.

## Reporting

Every metric, interval and latency percentile is reported per dataset. A latency figure pooled over datasets is labelled secondary. Each number has a 95% percentile bootstrap interval over queries: 10,000 resamples, seed 2026, nearest-rank endpoints. The interval covers the choice of queries only, not the corpus draw, so for the sampled corpora it understates the uncertainty about the dataset's full-corpus number. Results are published whichever way they fall, beside each dataset's published CoIR figures, with these caveats next to them: the sampled corpus (easier), the duplicate merge (higher), chunk-level retrieval, and warm client-side latency.

## The decision taken from the result

**[Sam to state before the run.]**
