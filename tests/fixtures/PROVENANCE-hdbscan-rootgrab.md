# `hdbscan_rootgrab_400x1024_f16.npz`

Frozen fixture backing `test_cluster_caps_giant_cluster`
(tests/db/test_taxonomy_compute.py, nexus-9b9oi).

## Why it is frozen

HDBSCAN's default `eom` cluster selection root-grabs on real embedding
geometry: on 2026-08-14 the freshly reindexed
`knowledge__delos__voyage-context-3__v1` (2,101 chunks from 15
distributed-systems papers) came back as 2 topics, one holding 94% of the
collection. Synthetic shapes provably do NOT reproduce the failure —
well-separated Gaussian blobs cluster cleanly, undifferentiated blobs go
all-noise, and blob-plus-background hierarchies still separate — so the
regression pin must ride real embedding geometry.

## What it is

A 400-vector subsample of that collection's stored embeddings
(`voyage-context-3`, 1024-dim, unit-norm), selected with
`numpy.random.default_rng(42).choice(2101, 400, replace=False)` and stored as
float16 (732 KB). The float16 roundtrip was verified to preserve the failure
shape.

Behavior on this fixture (sklearn `HDBSCAN(min_cluster_size=5)`):

| configuration | clusters | sizes |
|---|---|---|
| uncapped (pre-fix) | 2 | [359, 23] |
| `max_cluster_size=_max_cluster_size(400)` | 16 | largest 40 |

The vectors are embeddings of text chunks from published academic papers
(the Delos bibliography: Fireflies, Rapid, Aleph-BFT, etc.); no private
content. Embeddings are not invertible to text.

## Regenerating

Re-fetch embeddings for any real prose collection that exhibits the
degenerate 2-topic discovery, subsample to ~400 with a fixed seed, cast to
float16, and verify the uncapped run still produces a >85%-of-n cluster
before replacing this file.
