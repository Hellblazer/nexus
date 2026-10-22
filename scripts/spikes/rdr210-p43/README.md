# RDR-210 P4.3 ranking measurement (nexus-t189m)

The harness for the CA-2 residuals of RDR-210 (`docs/rdr/rdr-210-dual-mode-local-engine.md`
:544-590, Technical Design 12 :791-830, the step :1653-1666). The 210-research-16/-17 spike
scripts and their query set are gone; this rebuilds them over a fixed, committed query set and
runs them through the engine's HTTP search route and the real nexus search code, never a
reimplementation of its scoring.

It answers three questions on a dual (bge-768 + Voyage) local engine:

| | question | pass |
|---|---|---|
| **A** | Is the bge calibration factor still about 1.38? | the factor's 95% bootstrap interval overlaps 1.36-1.48 |
| **B** | Are the local cross-encoder's scores comparable across requests (so per-model rerank groups can be merged by `rerank_score`)? | split-merge Kendall tau within 0.01 of the noise floor |
| **C** | How many queries get a cross-family inversion on each surface, and at which bge factor? | counts no worse than the P1.5 fixture bound, 3 and 1 of 20 |

If A fails, or C is worse than the bound at the value the table holds, Technical Design 12 is
revised before Phase 6 (the bead's fail condition). Record the result as a research finding.

## Run it

```bash
scripts/spikes/rdr210-p43/run.sh --plan                 # corpora, queries, collections, Voyage token estimate; calls nothing
scripts/spikes/rdr210-p43/run.sh --keyless              # B only, on a bge-only engine; no key, no Voyage, no spend
scripts/spikes/rdr210-p43/run.sh                        # A, B and C on a keyed dual engine
scripts/spikes/rdr210-p43/run.sh --cap-tokens 2500000   # raise the spend cap (default 1,500,000)
scripts/spikes/rdr210-p43/run.sh --clean                # also delete the work dir at the end
```

Other flags go to `p43_run.py`: `--only A,B,C`, `--limit-queries N` (smoke test, results marked
partial), `--resamples`, `--seed`, `--depths 10,20`, `--no-shuffled-collection`,
`--assume-voyage-rerank` (count rerank tokens in `--plan`).

`run.sh` provisions a throwaway install the way `tests/e2e/dual-embedder-gate.sh` does: an empty
fenced `HOME`, a PATH shim that refuses a bare `nx`, `nx init --service` for a private Postgres, the
stamped gate jar from `scripts/build-gate-jar.sh` (or `NEXUS_SERVICE_BIN`, or `NX_P43_LAB_RUN_CLASS=lab-m`
to build inside a lab-run grant). On a lab host run the whole script as `lab-run -c lab-m -- ...`.

**The key** (full run) comes from the same chain as the gate (`NX_VOYAGE_API_KEY`, `VOYAGE_API_KEY`, the
repo `.env`, `credentials.voyage_api_key` in `~/.config/nexus/config.yml`). It reaches the engine through the
environment of the daemon-start subshell only, is never written to the scratch config, is never printed, and
never reaches the Python harness. No key means exit 1 (never a skip); `--keyless` is the keyless mode.

**The seed boot.** A Voyage collection can only be registered while the install's profile is Voyage (a bge
profile answers 422 to a new Voyage collection). So the first full run boots once with
`local.embed_model` set to a Voyage model, registers the prose and code Voyage collections, stops, switches the
config to bge, and boots the engine under test keyed with the bge profile (`embedding_mode: dual`). Every later
run skips the seed boot.

**Idempotent re-runs.** The work dir (`$P43_WORKDIR`, default `${TMPDIR:-/tmp}/nx-local-service-gate.p43`) persists:
the scratch Postgres, `p43-state.json` (per document: catalog id, done), and `results/`. Collections are named
from the corpus content hash (`p43-<hash8>`); a collection is reused when the engine's own chunk count equals the
number of distinct chunk hashes, and a failed run resumes at the first unfinished document. Change a corpus file
and the hash, hence the collection names, change. `--clean` (or deleting the dir) starts over.

## What it builds

* **Prose corpus**: 46 Markdown files (21 from `docs/*.md`, 25 from `docs/rdr/rdr-*.md`), 1,717 chunks. Written to
  `knowledge__p43-<h>__bge-base-en-v15-768__v1` and `knowledge__p43-<h>__voyage-context-3__v1`.
* **Code corpus**: 40 Python files from `src/nexus`, 877 chunks. Written to `code__p43-<h>__bge-base-en-v15-768__v1`
  and `code__p43-<h>__voyage-code-3__v1`.
* The file lists are chosen deterministically (sha1 of the path orders the candidates) and pinned in
  `corpus-files.json` with each file's sha256; a changed file prints a drift warning and makes new collections.
* Chunking (`p43_chunker.py`): paragraphs and headings, at most 1500 characters, deterministic. A heading starts a
  chunk; other blocks pack greedily.
* **Queries** (`queries.json`, fixed): 20 known-item questions per corpus, each with the one target file. They were
  written by reading the target and asking what someone who remembers the document but not its words would ask; a unit
  test fails if nine consecutive query words appear verbatim in the target. A chunk is relevant when it comes from the
  target file (identical text in another file is relevant to both).
* Both families are written through the combined write `POST /v1/catalog/manifest/write_many` (chunks and owner in one
  transaction), batched at the real client's per-family caps (bge 16, voyage-context-3 64, voyage-code-3 300) with
  `/manifest/append` for the rest of a long document. The bge and Voyage copies are separate catalog documents: a
  document moves whole to the collection it is written into (CA-4), so one document cannot live in both.

## What each number means

### A. Calibration factor (`measurement_a`)

For each query, the top 10 chunks of each collection by raw distance (`/v1/vectors/search`, no rerank, no
threshold), labelled relevant or not. One **global** threshold per family (all 20 queries pooled) that maximises F1.
The factor is **0.45 / bge threshold**: voyage-code-3's relevance boundary is 0.45, so a bge distance times the
factor lands on that scale. Reported per set (`prose`, `code`, `pooled`) with:

* `threshold`, `f1`: the best-F1 threshold for the family and its F1;
* `threshold_ci95`, `factor_ci95`: seeded bootstrap (1000 resamples of the queries, seed 210); the factor interval is
  0.45 over the threshold interval, ends swapped;
* the Voyage family's own threshold beside it (210-research-17 found 0.641 for context-3, the recorded 0.65);
* `pool_basis` (headline): a miss is a relevant candidate above the threshold within the pooled top 10s.
  `corpus_basis`: a miss is any relevant chunk of the target file, retrieved or not; its F1 is lower and its
  threshold higher, shown so the choice of basis is visible;
* `distances`: relevant p50/p90 and off-topic medians, as research-17 reported.

**Verdict**: PASS when the bge factor's interval overlaps 1.36-1.48; per set and pooled (pooled is the headline). If
it does not, the output says FAIL and shows the interval; nothing is rounded into a pass.

### B. Cross-request rank stability (`measurement_b`)

Per query a pool of 60 chunks (up to 8 from the target file, the rest the nearest other chunks by bge distance),
built as collections: **X** all 60, **A** and **B** a seeded 30/30 split of the same texts, **X'** the 60 inserted in a
shuffled order. Each is searched with `rerank=true` and a limit at or above the pool, so every chunk is scored.

* `split_merge`: A and B reranked in separate requests and merged by `rerank_score`, against the single request on X.
* `noise_floor_shuffled_pool`: X against X' (the pool order shuffled, the closest the HTTP route allows; the route
  orders candidates by distance, so only the physical insertion order and the index traversal differ).
  `noise_floor_repeat`: X against X again (run-to-run noise).
* Per set: Kendall tau-b of the scores (`tau_mean`, `tau_min`), `top1_same` out of N, top-10 overlap, and the mean and
  max absolute score difference. A cross-encoder scoring each pair on its own gives tau 1.0 and a difference near zero
  at both the split and the noise floor.

**Verdict**: PASS when `noise tau_mean - split tau_mean <= 0.01`. The result records `rerank_models_seen`: a
bge-profile engine reranks with the local cross-encoder (`ms-marco-minilm-l6-v2`) even when keyed; a Voyage-profile
engine reranks with Voyage and B then spends tokens (the budget charges them).

### C. Cross-family inversions (`measurement_c`)

The same corpus sits in a bge and a Voyage collection, so for each query both families hold the relevant chunk.
**dir1** = an irrelevant bge chunk ranks above the best relevant Voyage chunk (it sinks a Voyage hit); **dir2** = an
irrelevant Voyage chunk ranks above the best relevant bge chunk. Counts are queries out of 20 per corpus, at list
depths 10 and 20 (`--depths`); `*_relevant_missing` counts queries whose list held no relevant chunk of that family
(that direction cannot be judged there). Reference: research-17 counted 12 and 5 of 20 at the old default factor 0.818,
7 and 2 at 1.0, and 3 and 1 at 1.15-1.30. Those used a different pairing (bge docs against Voyage rdr), so the numbers
compare in kind, not exactly.

| surface | driven through |
|---|---|
| 1 `nx search` rerank on | `commands.search_cmd.search_cmd` over both collections (server rerank, `rerank_score` merge, file-diversity cap) |
| 2 MCP `query`, plain path | `search_engine.search_cross_corpus` then `apply_ranking_boosts` (the calibrated ordering), rerank off |
| 3 catalog-routed tools' merge | `mcp.core._grouped_combined_query` over `search_metadata_scoped` |

Surface 2 stops short of `query()`'s document grouping on purpose: its identical-chunk collapse (GH #1524) would
drop the second family's copy of every chunk, and here both families hold the same texts. Surface 3 is the merge
function itself, at chunk level.

`factor_grid_surface_2` and `factor_grid_surface_3` re-order the same retrieved rows at bge factors 1.0 to 1.6
(step 0.1) by patching the calibration table the real code reads; `calibration_table_bge_factor` is the value the
table holds in the tree under test (1.30 interim). **Pass**: counts on every surface no worse than 3 and 1 of 20
(`within_p1_5_bound`). The nexus-ndr0t critic noted that 3 and 1 may not be reachable at 1.38 on all pairings; the grid
shows where, if anywhere, the bound holds.

## Output

`$P43_WORKDIR/results/results.json` (strict JSON: raw per-query candidate lists and rankings, every per-query
statistic, the engine's `/version`, corpus hashes and file lists, the token ledger) and `summary.txt`, a plain-text
table that is also printed at the end. `tokens` holds the estimate charged (characters / 3.3, charged before each
send) and the engine-reported `X-Nexus-Usage-Tokens` sum beside it.

## The Voyage cap

Every Voyage-bound step (corpus embeds, query embeds on Voyage collections, rerank when the engine's reranker is
Voyage) is charged to a budget before it is sent. `--plan` prints the estimate and exits 3 over the cap without
calling anything; a run checks the same plan first and stops mid-run if a charge would cross the cap. Raise it only
if that spend is intended: `--cap-tokens N`.

## Tests

`tests/test_rdr210_p43_harness.py` covers the pure parts: chunker determinism, Kendall tau, the seeded bootstrap, the
best-F1 threshold, the inversion counter, pool building, the cap refusal, the fixed query set against its corpora.
`NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_rdr210_p43_harness.py -q`.
