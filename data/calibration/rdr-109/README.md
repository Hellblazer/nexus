# RDR-109 Phase 4 calibration data

Held-out QA + boost-weight sweep artefacts for the salience-boost work.
The harness is `scripts/rdr-109-calibrate.py` at the repo root.

## Layout

- `seed_queries_<content_type>.json` — small (3-5) seed-query sets per
  content type. Used by the salient-sentence extractor prototype to
  generate per-chunk salience candidates.
- `qa_<content_type>.jsonl` — held-out questions with verified
  expected chunk chashes. JSONL schema:
  ```json
  {"question": "...", "expected_chunk_chash": "abc123...", "content_type": "..."}
  ```
- `results.md` — sweep outcomes per content type + chosen weight.
- `results.json` — machine-readable sweep results.

## Phase 4 status

This commit lands **infrastructure only**: layout, seed queries, the
calibration harness, prototype salient-sentence extractor, and a small
synthetic QA seed. The acceptance criterion of "≥30 verified Q&A per
content_type" (≥120 total) is tracked separately as a follow-up.

See `nexus-n3qu.4b` for the QA expansion work.

## Reproducibility

```bash
python scripts/rdr-109-calibrate.py \
    --content-type knowledge \
    --weights 0.0,0.025,0.05,0.075,0.10,0.15 \
    --top-k 5
```

The harness reads `seed_queries_<ct>.json` and `qa_<ct>.jsonl`, runs
`search_cross_corpus` for each weight, computes top-K hit rate, and
emits a per-weight table to stdout + a JSON record to `results.json`.

## Boost mechanism (prototype)

Phase 5 will ship the production version. Phase 4's prototype is in
`scripts/rdr_109_salience.py`:

1. **Salient-sentence extraction.** For each chunk, split into
   sentences, score each `(seed_query, sentence)` pair with the
   Phase 3 cross-encoder, retain the top-N highest-scoring sentences
   as the chunk's salience candidates.
2. **Token-overlap boost.** At search time, compute token-set overlap
   between the user query and each candidate chunk's stored salient
   sentences. Add `weight * overlap_fraction` to the hybrid score.

The harness wires both stages so the sweep can measure end-to-end hit
rate impact at varying weights.
