---
title: "bge-768 as the Local-Mode T3 Embedder in the Java Service: Replace MiniLM-384 ONNX, Parity-Gated Against fastembed"
id: RDR-160
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-15
related_issues: [nexus-jrrve, nexus-vwvv5, nexus-luxe6]
related: [RDR-144, RDR-152, RDR-155, RDR-157]
---

# RDR-160: bge-768 as the Local-Mode T3 Embedder in the Java Service

## Context / Problem

Since RDR-152 (`nexus-gmiaf.20`, server-side embedding) the Java storage service's
ONLY local embedder is `OnnxEmbedder`, which loads **MiniLM-384**
(`all-MiniLM-L6-v2`, 384-dim) from `~/.cache/chroma/onnx_models/...`. The
`EmbedderRouter` local-mode constructor wires that single embedder for **every**
collection. There is no bge-768 embedder anywhere in the service
(`service/src/main/java/dev/nexus/service/vectors/`), even though the storage
schema already supports it (`PgVectorRepository` maps `bge-base-en-v15-768 →
chunks_768`).

Meanwhile the Python onboarding (`nx init`, RDR-144) presents **bge-768 as the
RECOMMENDED local embedder**. So the two halves disagree: the user is told bge-768
is the local default, but the actual T3 backend silently embeds everything with
MiniLM-384. RDR-155 explicitly noted "local = MiniLM 384 **or** bge-base" and
shipped MiniLM; bge-768 for the service was never implemented.

Owner decision (2026-06-15): **bge-768 is the local-mode T3 default; MiniLM-384 is
strictly for T1** (the Python-side chromadb scratch tier, not the Java service).
The service must embed local T3 with bge-768 via ONNX.

This also subsumes `nexus-jrrve` — the "fresh `nx init --service` boot-fails because
nothing fetches the ONNX model" bug. That boot failure is a symptom of the deeper
issue (wrong/unprovisioned local embedder); the fix is to provision the *correct*
model (bge-768), not the wrong one (MiniLM-384).

**Greenfield (load-bearing).** The service stack is pre-release (develop
unreleasable since RDR-155 P4a, `nexus-luxe6`). There are no production local-mode
(no-Voyage) T3 collections embedded with MiniLM-384 to strand. Fixing the default
NOW — before any local user exists — costs nothing; deferring creates a forced
re-embed for every future local user.

## Decision

1. **The Java service's local embedder is ONNX-runtime loading bge-base-en-v1.5
   (768-dim).** The embedding RUNTIME stays ONNX (onnxruntime-java + DJL
   tokenizer, as today); only the MODEL changes from MiniLM-384 to bge-768.
   MiniLM-384 is **dropped from the service**.
2. **`EmbedderRouter` local mode routes all collections to the bge-768 embedder**
   (model token `bge-base-en-v15-768`, dim 768, table `chunks_768`).
3. **Parity gate (the load-bearing correctness check).** The Java bge embedder's
   output must match the Python **fastembed** bge reference within tolerance,
   modeled on the existing `EmbedParityTest` / parity gate (`nexus-gmiaf.21`). bge
   preprocessing differs from MiniLM: **CLS pooling + L2-normalize, no instruction
   prefix** (verified: `nexus.db.local_ef` calls fastembed `TextEmbedding.embed()`
   on raw input — no `"Represent this sentence…"` prefix). MiniLM used
   mean-pooling; getting bge's pooling/normalization wrong silently produces
   incomparable vectors, so the parity gate is mandatory, not optional.
4. **The CLI fetches, the service reads.** `nx init --service` warms the bge ONNX
   model into a stable, Java-loadable path (the CLI is the network-facing side;
   the service only reads the cached file — consistent with the topology
   invariant that the local Java service makes no outbound HTTP). This reuses the
   warmup mechanism prototyped under `nexus-jrrve`.
5. **MiniLM-384 stays only as the T1 Python chromadb default** (untouched there).
   It is removed from the Java service's local T3 path.
6. **Greenfield: no migration.** Folds `nexus-jrrve` as a symptom.

## Approach (phased)

1. **P1 — Java bge-768 ONNX embedder + parity gate (TDD).** Add a bge embedder
   (new class, or generalize `OnnxEmbedder` to take a model token + pooling
   strategy) that loads the bge ONNX + tokenizer, applies **CLS pooling +
   L2-normalize**, emits 768-dim, `modelToken() = "bge-base-en-v15-768"`. Write the
   **parity test FIRST** against a captured fastembed bge reference (model the
   harness on `EmbedParityTest` / `gmiaf.21`): identical inputs → cosine ≈ 1.0
   within tolerance. This gate is the go/no-go for the whole RDR.
2. **P2 — Router rewire + drop MiniLM from the service.** `EmbedderRouter`
   local-mode constructor wires the bge embedder for all collections; `Main.java`
   constructs it; route to `chunks_768`. Remove the MiniLM `OnnxEmbedder` from the
   service local path (decide P0: delete the class vs keep dormant — see Open Q).
3. **P3 — Warmup + distribution.** `nx init --service` (local mode) fetches the bge
   ONNX + tokenizer to the Java-read path (fastembed downloads it; expose/copy to a
   stable location the service loads). Reconcile with **RDR-157**: its P4
   "local mode requires the bundled ONNX MiniLM model — depends on nexus-jrrve"
   flips to bge-768 (the RDR-157 PG bundle is unaffected; only the embedder model
   changes). Update RDR-157 §Approach P4 accordingly.
4. **P4 — Close-out.** phase-review-gate cross-walk + stacked review (code-review-
   expert + substantive-critic) of the parity gate and router rewire; close
   `nexus-jrrve` as subsumed.

## Critical Assumptions (P0 — verify in research before P1)

- **CA-1 (parity feasibility).** onnxruntime-java + DJL `HuggingFaceTokenizer` can
  load the bge-base-en-v1.5 ONNX model fastembed uses, and CLS-pool + L2-normalize
  in Java reproduces fastembed's vectors within tolerance. (Highest risk; the
  whole RDR rests on it.)
- **CA-2 (preprocessing identity).** fastembed bge-base-en-v1.5 applies CLS pooling
  + normalize and NO instruction prefix by default. Partially verified
  (`local_ef` passes raw input); confirm fastembed's internal pooling/normalization
  so the Java side matches exactly.
- **CA-3 (model availability).** The bge ONNX + `tokenizer.json` land at a stable
  path the Java service can load (fastembed cache layout, or a fetch/copy step in
  the warmup). Determines the P3 distribution mechanism.

## Alternatives Considered

- **Keep MiniLM-384 as the local service embedder.** Rejected: contradicts the
  owner decision and ships materially worse local search quality than the bge-768
  the onboarding promises.
- **Run fastembed inside the JVM.** Rejected: no maintained JVM fastembed; the
  service already embeds via onnxruntime-java — loading the bge ONNX directly is
  the established pattern.
- **Bundle bge ONNX into the native image (`-H:IncludeResources`, ~140 MB).**
  Deferred to RDR-157 distribution mechanics; orthogonal to this embedder change
  (the warmup-fetch path works for both bundled and ship-alongside).

## Consequences

- The local distribution (RDR-157) carries/fetches the bge ONNX (~140 MB) instead
  of MiniLM-384; the PG bundle is unaffected.
- `chunks_384` is unused on the local path; MiniLM-384 survives only as the T1
  Python default.
- Any future need for a 384-dim service path (e.g. a deliberately-cheap tier) would
  re-introduce a model token + parity gate — the P1 generalization should leave
  that seam clean.

## Open Questions

1. Remove the MiniLM `OnnxEmbedder` class from the service entirely, or keep it
   dormant (un-wired) for a possible future cheap tier? (Leaning: keep the ONNX
   embedder generalized by model token; un-wire MiniLM from the local default.)
2. Distribution: does the native-image local distribution bundle the bge ONNX or
   fetch on first run? (Defer the mechanism to RDR-157; this RDR only requires the
   model reach a Java-loadable path.)
