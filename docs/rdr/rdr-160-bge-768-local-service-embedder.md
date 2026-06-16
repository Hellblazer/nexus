---
title: "bge-768 as the Local-Mode T3 Embedder in the Java Service: Replace MiniLM-384 ONNX, Parity-Gated Against fastembed"
id: RDR-160
type: Architecture
status: accepted
accepted_date: 2026-06-15
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-15
related_issues: [nexus-jrrve, nexus-vwvv5, nexus-luxe6]
related: [RDR-144, RDR-152, RDR-155, RDR-157]
---

# RDR-160: bge-768 as the Local-Mode T3 Embedder in the Java Service

## Problem Statement

Owner decision (2026-06-15): **bge-768 is the local-mode T3 default; MiniLM-384 is
strictly for T1** (the Python-side chromadb scratch tier, not the Java service). The
service must embed local T3 with bge-768 via ONNX. Today it does not. Three gaps.

#### Gap 1: The Java service's local embedder is MiniLM-384, not bge-768

Since RDR-152 (`nexus-gmiaf.20`, server-side embedding) the Java storage service's
ONLY local embedder is `OnnxEmbedder`, which loads **MiniLM-384**
(`all-MiniLM-L6-v2`, 384-dim) from `~/.cache/chroma/onnx_models/...`. The
`EmbedderRouter` local-mode constructor wires that single embedder for **every**
collection. There is no bge-768 embedder anywhere in the service
(`service/src/main/java/dev/nexus/service/vectors/`), even though the storage
schema already supports it (`PgVectorRepository` maps `bge-base-en-v15-768 →
chunks_768`). RDR-155 noted "local = MiniLM 384 **or** bge-base" and shipped MiniLM;
bge-768 for the service was never implemented.

#### Gap 2: The onboarding promises bge-768 that the backend does not deliver

The Python onboarding (`nx init`, RDR-144) presents **bge-768 as the RECOMMENDED
local embedder** and persists that choice. But the actual T3 backend silently embeds
everything with MiniLM-384. The user is told one thing and gets another — a silent
quality downgrade (384-dim MiniLM vs the 768-dim bge they chose).

#### Gap 3: Migration compatibility — existing 5.x.x bge-768 data needs a bge-768 service

The released 5.x.x series embeds local T3 with **bge-768** (fastembed, ChromaDB
path). When that data migrates into the service's pgvector, the service MUST embed
with bge-768 too, or migrated vectors (768-dim bge) and service-produced vectors
(384-dim MiniLM) live in different spaces and search breaks. This makes bge-768 in
the service a correctness requirement for the upgrade path, not merely a quality
preference. (Also subsumes `nexus-jrrve`: the "fresh `nx init --service` boot-fails
because nothing fetches the ONNX model" bug is a symptom — the fix is to provision
the *correct* model, bge-768, not MiniLM-384.)

**Greenfield (load-bearing).** The service stack is pre-release (develop unreleasable
since RDR-155 P4a, `nexus-luxe6`). There are no production local-mode (no-Voyage) T3
collections embedded with MiniLM-384 *in the service* to strand. Fixing the default
NOW — before any local service user exists — costs nothing; deferring creates a
forced re-embed for every future local user.

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
4. **The CLI fetches, the service reads.** `nx init --service` provisions a
   **standard (un-fused) bge ONNX** + tokenizer into a stable, Java-loadable path
   (the CLI is the network-facing side; the service only reads the file —
   consistent with the topology invariant that the local Java service makes no
   outbound HTTP). NOTE (CA-1/RF-160-1): this is NOT fastembed's cached
   `model_optimized.onnx` (that fails to load on onnxruntime-java); the service
   needs its own standard export. The CLI-fetches/service-reads *mechanism* from
   `nexus-jrrve` is reused; the model artifact differs.
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
3. **P3 — Warmup + distribution + onboarding reconciliation.** `nx init --service`
   (local mode) fetches the **standard fp32** bge ONNX + tokenizer to the Java-read
   path. NOTE (CA-1): this is NOT fastembed's cached `model_optimized.onnx` (it fails
   to load on onnxruntime-java); the CLI fetches a standard export and copies it to a
   stable location the service loads. Reconcile with **RDR-157** §Approach P4
   ("local mode requires the bundled ONNX MiniLM model — depends on nexus-jrrve") →
   bge-768 (PG bundle unaffected; model is fetched separately at ~416 MB, not part of
   the PG archive); that RDR-157 edit is made under this RDR. **Also reconcile
   RDR-144 onboarding:** for SERVICE installs the embedder choice is locked to
   bge-768 with an advisory — the minilm-384 onboarding option is non-operative for
   the service T3 path (the service routes all collections to bge-768 regardless), so
   a silently-ignored minilm-384 choice must not be presented. (minilm-384 remains a
   valid T1 Python choice; only the service-install path is locked.)
4. **P4 — Close-out.** phase-review-gate cross-walk + stacked review (code-review-
   expert + substantive-critic) of the parity gate and router rewire. **Dispose of
   the existing MiniLM `EmbedParityTest` (`nexus-gmiaf.21`):** retire it for the
   service path or re-scope it to the T1 Python MiniLM context — do not leave it to
   pass vacuously or break silently when MiniLM is un-wired from the service. Close
   `nexus-jrrve` as subsumed.

## Critical Assumptions (P0)

- **CA-1 (parity feasibility) — VERIFIED 2026-06-15 (RF-160-1).** onnxruntime-java
  1.20.0 + DJL `HuggingFaceTokenizer` 0.30.0 reproduce fastembed bge-768 at **min
  cosine 0.99999229** across 5 texts (incl. empty + multiline/unicode), well above
  the 0.9999 gate. Proven by `Bge768ParitySpikeTest`. **Caveat (load-bearing):**
  fastembed ships qdrant's `model_optimized.onnx`, which uses the MS contrib fused
  op `SkipLayerNormalization` that onnxruntime-java 1.20.0 **cannot run**
  (`ORT_RUNTIME_EXCEPTION … Missing Input … LayerNorm.weight`). The service MUST use
  a **standard (un-fused) bge ONNX export** (core ops only). A standard export
  (Xenova/bge-base-en-v1.5 `model.onnx`) matches fastembed's optimized output at
  cosine 0.999992 in Python (fusion is numerically equivalent), and the Java spike
  on that standard model passes.
- **CA-2 (preprocessing identity) — VERIFIED.** Recipe is **CLS pooling (token 0) +
  L2-normalize**, 512-token truncation, `token_type_ids` all-zero, **no prefix**.
  Python raw-onnxruntime: CLS+norm = cosine 1.000000 vs fastembed; mean-pool = 0.825
  (confirms it is NOT MiniLM-style mean-pooling).
- **CA-3 (model source/distribution) — RESOLVED 2026-06-15 (RF-160-2): standard
  fp32.** Measured all Xenova bge ONNX variants vs the fastembed reference:
  `model.onnx` (fp32, 416 MB) = **0.999992**; every standard *quantized* variant
  (int8/uint8/q4/bnb4, 105–142 MB) scores **0.95–0.99 — below the 0.9999 gate**;
  fp16 variants fail to load on this ORT. Insight: fastembed's own model is qdrant's
  quantized `-onnx-q` (the ground truth existing 5.x.x bge-768 data was embedded
  with); the fp32 standard matches it at 0.999992, while standard-quantized uses a
  *different* quant scheme and diverges — so quantizing the service model would
  break the very parity this RDR exists to preserve. **Decision: ship/fetch the
  standard fp32 bge ONNX (~416 MB) + `tokenizer.json`.** Distribution (one-time
  fetch, not hot path) is handed to RDR-157 local-distribution mechanics. A
  calibrated int8 export that re-verifies ≥0.9999 is a possible size follow-up, not
  baseline.

## Alternatives Considered

- **Keep MiniLM-384 as the local service embedder.** Rejected: contradicts the
  owner decision and ships materially worse local search quality than the bge-768
  the onboarding promises.
- **Run fastembed inside the JVM.** Rejected: no maintained JVM fastembed; the
  service already embeds via onnxruntime-java — loading the bge ONNX directly is
  the established pattern.
- **Bundle bge ONNX into the native image (`-H:IncludeResources`, ~416 MB fp32).**
  Deferred to RDR-157 distribution mechanics; orthogonal to this embedder change
  (the warmup-fetch path works for both bundled and ship-alongside). Note the fp32
  size makes ship-alongside / first-run fetch more attractive than in-binary embed.

## Consequences

- The local distribution (RDR-157) carries/fetches the standard **fp32** bge ONNX
  (**~416 MB** — quantized variants were ruled out by the parity gate, CA-3) instead
  of MiniLM-384. It is fetched SEPARATELY at first run, not part of the ~6 MB PG
  archive; the PG bundle itself is unaffected.
- `chunks_384` is unused on the local path; MiniLM-384 survives only as the T1
  Python default.
- Any future need for a 384-dim service path (e.g. a deliberately-cheap tier) would
  re-introduce a model token + parity gate. P1 implements `Bge768Embedder` as a
  distinct class (Open Q1 resolved), leaving the MiniLM `OnnxEmbedder` pristine; the
  `EmbedderRouter` generalization that makes the seam fully clean (widening its
  local-mode field/constructor from the concrete `OnnxEmbedder` to the `Embedder`
  interface) is **P2** work, done when bge-768 is wired into `modelEmbedders`. P1
  does not touch the router.

## Open Questions

1. Remove the MiniLM `OnnxEmbedder` class from the service entirely, or keep it
   dormant (un-wired) for a possible future cheap tier? (Leaning: keep the ONNX
   embedder generalized by model token; un-wire MiniLM from the local default.)
2. Distribution: does the native-image local distribution bundle the bge ONNX or
   fetch on first run? (Defer the mechanism to RDR-157; this RDR only requires the
   model reach a Java-loadable path.)
