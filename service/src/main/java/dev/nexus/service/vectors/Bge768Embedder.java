// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.djl.huggingface.tokenizers.Encoding;
import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;
import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtException;
import ai.onnxruntime.OrtSession;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.LongBuffer;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-160 (bead nexus-1chpa) — LOCAL bge-768 ONNX embedder for the Java service.
 *
 * <p>The service's local-mode T3 embedder. Loads {@code BAAI/bge-base-en-v1.5}
 * (768-dim) via onnxruntime-java + DJL HuggingFace tokenizer and reproduces the
 * Python <b>fastembed</b> {@code TextEmbedding.embed()} output within tolerance
 * (parity gate {@link dev.nexus.service.Bge768ParityTest}, min cosine ≥ 0.9999).
 *
 * <p><b>Recipe (CA-2, VERIFIED — do not deviate):</b>
 * <ol>
 *   <li>HuggingFace tokenize with {@code truncation=true, maxLength=512} via DJL</li>
 *   <li>ONNX inputs: {@code input_ids}, {@code attention_mask}, and
 *       {@code token_type_ids} (zeros) <em>iff the model declares it</em></li>
 *   <li>Output[0] = {@code last_hidden_state} shape [batch, seq, 768]</li>
 *   <li><b>CLS pooling</b>: take token 0 of {@code last_hidden_state}
 *       (NOT MiniLM's masked mean-pool)</li>
 *   <li>L2-normalize each vector</li>
 * </ol>
 *
 * <p><b>No instruction prefix.</b> fastembed embeds raw input; the
 * {@code "Represent this sentence…"} query prefix is NOT applied (it would break
 * parity). Verified against {@code nexus.db.local_ef}.
 *
 * <p><b>Model artifact (CA-1 caveat, RF-160-1):</b> this loads a STANDARD
 * (un-fused) bge ONNX export (Xenova/bge-base-en-v1.5 {@code model.onnx}, fp32).
 * It must NOT be pointed at fastembed's cached qdrant {@code model_optimized.onnx}
 * — that uses the fused MS contrib op {@code SkipLayerNormalization} which
 * onnxruntime-java 1.20.0 cannot execute. {@code nx init --service} (RDR-160 P3)
 * provisions the standard export to {@link #DEFAULT_MODEL_PATH}.
 *
 * <p><b>Design (Open Q1):</b> implemented as a distinct class rather than by
 * generalizing {@link OnnxEmbedder}, keeping the MiniLM-384 pipeline pristine for
 * the T1 Python seam. A future shared base can factor the common tensor plumbing
 * if a third ONNX model arrives.
 *
 * <p>Thread-safe: {@link OrtSession} and {@link HuggingFaceTokenizer} are kept in
 * fields, both documented thread-safe for inference / encode.
 */
public final class Bge768Embedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(Bge768Embedder.class);

    /** Canonical Java-read path for the standard bge ONNX (provisioned by {@code nx init --service}).
     * Root resolved via {@link OnnxModelPaths} (nexus-ogccs): env override, then HOME —
     * the provisioner writes under HOME, and bare {@code user.home} (the passwd entry)
     * diverges from it whenever HOME is overridden. */
    public static final String DEFAULT_MODEL_PATH =
            OnnxModelPaths.root() + "/bge-base-en-v1.5/onnx/model.onnx";

    public static final String DEFAULT_TOKENIZER_PATH =
            OnnxModelPaths.root() + "/bge-base-en-v1.5/onnx/tokenizer.json";

    /** bge-base-en-v1.5 supports 512-token context (MiniLM used 256). */
    private static final int MAX_SEQ_LEN = 512;

    /**
     * bge-base-en-v1.5 is a BERT-base architecture: 12 transformer layers, 12
     * attention heads, 768 hidden dim. Confirmed against the nexus-33hpq/nexus-zu4ma
     * measurements below (not an assumed constant): a single layer's attention-score
     * tensor is {@code [batch, heads, seq, seq]} float32, and
     * {@code batch=64 * heads=12 * seq=512 * seq=512 * 4 bytes = 805,306,368 bytes
     * ≈ 0.81 GB} exactly matches the measured 0.81 GB/tensor at batch=64/seq=512
     * from nexus-33hpq's investigation, and scales linearly to the measured 3.77 GB
     * at batch=300/seq=512.
     */
    private static final int ATTENTION_HEADS = 12;

    /**
     * Sub-batch memory budget (nexus-zu4ma), expressed as bytes for ONE layer's
     * attention-score tensor at the group's own shape:
     * {@code groupBatchSize * ATTENTION_HEADS * maxLen^2 * BYTES_PER_FLOAT32}.
     *
     * <p>Bounding by "padded token area" ({@code groupBatchSize * maxLen^2}) rather
     * than by a flat row-count constant means one long chunk in a group raises that
     * group's {@code maxLen} and therefore shrinks how many rows fit alongside it —
     * a single 512-token chunk can never inflate every OTHER row's padding cost the
     * way a flat 300-row cap did (nexus-33hpq: 3.77 GB attention tensor, ~77.4 GB
     * observed peak RSS, engine wedged).
     *
     * <p>Sized at the nexus-33hpq VERIFIED-SAFE operating point — batch=16 at the
     * full 512-token cap (every row simultaneously at MAX_SEQ_LEN, the worst case
     * for a given batch size) — because that exact shape was measured end-to-end at
     * 3.03 GB peak RSS with {@code flush_concurrency=3} genuinely active (release-
     * sandbox shakedown, 11/11 passing, zero storage_service_unhealthy events), i.e.
     * roughly 1 GB actual peak per single {@code embed()} call at this shape. This
     * is therefore not a re-derived guess: it reuses the one point on the memory
     * curve this codebase has already proven safe under real concurrent load,
     * generalized from "batch=16 rows" to "16*512^2 units of padded token area" so
     * any batch/length combination reaching the same area gets the same ceiling —
     * e.g. a 128-row batch of short (~64-token) chunks is allowed the same area as
     * 16 rows at the 512-token cap, since 128*64^2 ≈ 16*256^2 &lt; 16*512^2.
     */
    private static final long MAX_ATTENTION_TENSOR_BYTES =
            attentionTensorBytes(16, MAX_SEQ_LEN);

    /**
     * Largest fraction of a group's tokens that may be padding before the planner starts a new
     * group: a group closes when {@code size * maxLen > (1 + MAX_PAD_WASTE) * realTokens}.
     * 0.25 from a simulation over 12,999 real chunks at 16 per request: padded tokens 1.62x ->
     * 1.12x of real, at about 4 ONNX calls per request instead of one. 0.1 reaches 1.04x but
     * needs about 6 calls per request, and a call that small starts to lose the arithmetic
     * efficiency the saved padding was meant to buy; 0.5 stays at 1.24x.
     */
    static final double MAX_PAD_WASTE = 0.25;

    private static long attentionTensorBytes(long batchSize, long seqLen) {
        return batchSize * ATTENTION_HEADS * seqLen * seqLen * Float.BYTES;
    }

    /** Equivalent padded-token-area ceiling ({@code batchSize * maxLen^2}) — heads
     * and bytes-per-float are constants that cancel out of the budget comparison,
     * so the sub-batch planner works in this unit directly rather than re-deriving
     * bytes on every candidate check. */
    private static final long MAX_PADDED_TOKEN_AREA =
            MAX_ATTENTION_TENSOR_BYTES / (ATTENTION_HEADS * (long) Float.BYTES);

    /**
     * Sanity floor for "this is the standard fp32 export, not a truncated download
     * or the ~140MB quantized/fused substitute". Mirrors the CLI's
     * {@code _MIN_MODEL_BYTES} (service_bge_model.py). The fp32 model is ~416MB.
     */
    private static final long MIN_MODEL_BYTES = 200_000_000L;

    /**
     * Bead nexus-s71lr: cap on how often {@code embedSubBatched} emits an INFO-level
     * progress line, shared across every concurrent {@code embed()} call on this instance
     * (see {@link EmbedProgressGate}'s javadoc for why global, not per-call). "About once
     * per 5 seconds" is the bead's own ask, sized against the reported incident (a 13-minute
     * bulk embed with zero log lines between the run's start and its per-document upsert
     * completions).
     */
    private static final long PROGRESS_LOG_INTERVAL_NANOS = java.util.concurrent.TimeUnit.SECONDS.toNanos(5);

    /**
     * Bead nexus-s71lr, deliverable 2: how long after the last completed sub-batch
     * {@link #activitySnapshot()} still reports {@code active=true}. Twice the log
     * interval above — long enough that a normal ~5s-cadenced bulk run reads
     * continuously active between log lines, short enough that a genuinely stalled
     * embed (the exact "13-minute silent hang" the bead reports) reads inactive
     * well before an operator would otherwise give up waiting.
     */
    private static final long ACTIVE_WINDOW_NANOS = 2 * PROGRESS_LOG_INTERVAL_NANOS;

    private final OrtEnvironment      ortEnv;
    private final OrtSession          session;
    private final HuggingFaceTokenizer tokenizer;
    /** Some bge ONNX exports declare {@code token_type_ids}, some don't — feed it only if present. */
    private final boolean             wantsTokenTypeIds;

    /**
     * Counts {@code session.run()} invocations — the non-vacuity instrument for the
     * sub-batch planner (nexus-zu4ma). Package-private read/reset so
     * {@link Bge768BatchCompositionTest}, in this same package, can prove an
     * oversize batch produced more than one ONNX call without any mocking
     * framework (mirrors this class's existing {@code clsPoolNormalize}
     * package-private test-access convention).
     */
    private final AtomicInteger onnxInvocationCount = new AtomicInteger(0);

    /** Bead nexus-s71lr: shared rate limiter for the embed-progress INFO log below. */
    private final EmbedProgressGate progressGate = new EmbedProgressGate(PROGRESS_LOG_INTERVAL_NANOS);

    /** Bead nexus-s71lr, deliverable 2: lifetime activity counters served by
     * {@code GET /v1/status} via {@link #activitySnapshot()}. */
    private final EmbedActivityTracker activityTracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);

    /**
     * Bead nexus-s71lr, code-review-expert pass 2 (finding c): "queue depth" +
     * "thread width" for the progress log line + {@link #activitySnapshot()},
     * READ from the admission gate rather than recomputed. Composition runs the
     * other way at construction time — {@code AdmissionControlledEmbedder} wraps
     * THIS class and holds the {@link LocalOnnxAdmission} reference, so this
     * embedder has no admission-gate reference of its own until one is wired in
     * post-construction. {@code null} (the default, and every direct test
     * construction of this class) means "no admission gate wired" — the log line
     * and {@link #activitySnapshot()} both omit/sentinel these two fields rather
     * than fabricate a value, never silently reporting 0.
     */
    private volatile LocalOnnxAdmission admissionGate;

    /**
     * Wire the process-wide admission gate this embedder is served through, so
     * the progress log line and {@link #activitySnapshot()} can report REAL
     * queue-depth/thread-width instead of omitting them. Called exactly once,
     * from {@code Main.java}'s local-mode branch, immediately after both this
     * embedder and the {@link LocalOnnxAdmission} it will be wrapped by are
     * constructed — composition-root wiring, not a service locator (this class
     * never looks the gate up itself).
     */
    public void setAdmissionGate(LocalOnnxAdmission gate) {
        this.admissionGate = gate;
    }

    /** Construct with the canonical bge artifact paths. */
    public Bge768Embedder() {
        this(DEFAULT_MODEL_PATH, DEFAULT_TOKENIZER_PATH);
    }

    /**
     * Construct with explicit paths (testing / non-default provisioning locations).
     *
     * @param modelPath     path to the standard un-fused bge {@code model.onnx}
     * @param tokenizerPath path to {@code tokenizer.json}
     */
    public Bge768Embedder(String modelPath, String tokenizerPath) {
        // Fail loud with a remedy BEFORE the opaque onnxruntime error: in local
        // mode this is the service's only embedder, and the ~416MB model is
        // provisioned separately by `nx init --service` (RDR-160 P3). A missing
        // file must name the path and the fix, not crash with an ORT stack trace.
        for (String[] req : new String[][]{
                {modelPath, "bge ONNX model"}, {tokenizerPath, "bge tokenizer"}}) {
            if (!java.nio.file.Files.isRegularFile(java.nio.file.Path.of(req[0]))) {
                throw new IllegalStateException(
                    "Bge768Embedder: " + req[1] + " not found at " + req[0]
                    + ". The local-mode service embeds with bge-base-en-v1.5 (768d); "
                    + "provision the STANDARD fp32 ONNX + tokenizer via `nx init --service` "
                    + "(RDR-160 P3), or point -Dnexus.bge.modelPath / -Dnexus.bge.tokenizerPath "
                    + "at an existing standard export (NOT fastembed's model_optimized.onnx).");
            }
        }
        // Size floor mirrors the CLI's _MIN_MODEL_BYTES (service_bge_model.py): the
        // standard fp32 export is ~416MB, so a model well under that is a truncated
        // download or the ~140MB quantized/fused substitute (CA-3 rejected). Catch
        // it here with a remedy rather than letting ORT fail opaquely at load.
        try {
            long bytes = java.nio.file.Files.size(java.nio.file.Path.of(modelPath));
            if (bytes < MIN_MODEL_BYTES) {
                throw new IllegalStateException(
                    "Bge768Embedder: model at " + modelPath + " is " + bytes
                    + " bytes — far below the standard fp32 bge export (~416MB). It looks "
                    + "truncated or a quantized/fused substitute (parity would silently "
                    + "degrade). Re-provision via `nx init --service` (RDR-160 P3).");
            }
        } catch (java.io.IOException e) {
            // Can't stat — don't block on a transient FS error; ORT load will surface it.
            log.warn("event=bge_model_size_check_failed path={} error={}", modelPath, e.getMessage());
        }

        // nexus-o5xyx.1: from the first ORT touch (getEnvironment creates ORT's logging
        // manager) until the session and tokenizer are built, process exit must wait —
        // a SIGTERM here otherwise tears the logging manager down under a live
        // InferenceSession::Initialize and the JVM SEGVs. Throws ShutdownInProgressException
        // (deliberately outside the catch below) if exit has already begun.
        OrtInitGate.Scope initScope = OrtInitGate.process().enter("bge768");

        OrtSession          sess = null;
        HuggingFaceTokenizer tok  = null;
        // SessionOptions is AutoCloseable; it holds no state once createSession returns.
        // getEnvironment() is the first statement INSIDE the try so the scope is closed by
        // the finally below on every path; nothing runs between enter() and the try.
        try (var sessionOpts = new OrtSession.SessionOptions()) {
            this.ortEnv = OrtEnvironment.getEnvironment();
            // nexus-00wsf: the intra-op width of this SHARED session comes from one
            // resolver (OnnxThreadPolicy: ORT's own default unless an operator overrides);
            // concurrency is bounded by LocalOnnxAdmission, never by this number.
            var intraOp = OnnxThreadPolicy.intraOpThreads();
            if (intraOp.isPresent()) {
                sessionOpts.setIntraOpNumThreads(intraOp.getAsInt());
            }
            sess = ortEnv.createSession(modelPath, sessionOpts);

            tok = HuggingFaceTokenizer.builder()
                    .optTokenizerPath(Path.of(tokenizerPath))
                    .optMaxLength(MAX_SEQ_LEN)
                    .optTruncation(true)
                    .optPadding(false)   // we pad per-sub-batch in runOnnxSubBatch() so each tensor is rectangular
                    .build();

            this.session = sess;
            this.tokenizer = tok;
            this.wantsTokenTypeIds = sess.getInputNames().contains("token_type_ids");

            log.info("event=bge768_embedder_loaded model={} tokenizer={} token_type_ids={}",
                    modelPath, tokenizerPath, wantsTokenTypeIds);
        } catch (Exception e) {
            // Don't leak the native OrtSession handle if tokenizer construction fails.
            if (tok != null)  { try { tok.close();  } catch (Exception ignored) {} }
            if (sess != null) { try { sess.close(); } catch (Exception ignored) {} }
            throw new RuntimeException("Failed to initialise Bge768Embedder: " + e.getMessage(), e);
        } finally {
            initScope.close();
        }
    }

    @Override
    public String modelToken() {
        return "bge-base-en-v15-768";
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        try {
            return embedSubBatched(texts);
        } catch (RequestDeadlineExceededException e) {
            // nexus-8hdg9 phase 3: rethrown UNWRAPPED so VectorHandler's typed 503 +
            // Retry-After arm sees it rather than the generic 500 arm.
            throw e;
        } catch (OrtInitGate.ShutdownInProgressException e) {
            // nexus-o5xyx.3: unwrapped, so VectorHandler answers a retryable 503.
            throw e;
        } catch (Exception e) {
            throw new RuntimeException("Bge768Embedder.embed failed: " + e.getMessage(), e);
        }
    }

    @Override
    public EmbedResult embedWithUsage(List<String> texts) {
        // ONNX is local-only: no API cost, no upstream usage counter. Emit tokens=0
        // (mirrors OnnxEmbedder) so the X-Nexus-Usage-Tokens header is not polluted.
        return new EmbedResult(embed(texts), 0L);
    }

    /**
     * Bead nexus-s71lr, deliverable 2 — a point-in-time snapshot of this embedder's
     * lifetime activity counters, served by {@code GET /v1/status}
     * ({@code dev.nexus.service.http.StatusHandler}) so a client can poll "is the
     * engine still embedding, or has it hung?" without tailing logs. Public: the
     * status handler lives in a different package.
     */
    @Override
    public EmbedActivitySnapshot activitySnapshot() {
        LocalOnnxAdmission gate = admissionGate;
        int queueDepth  = gate != null ? gate.queueLength() : -1;
        int threadWidth = gate != null ? gate.permits() : -1;
        return activityTracker.snapshot(System.nanoTime(), queueDepth, threadWidth);
    }

    /**
     * Bead nexus-8hdg9 — lets {@link AdmissionControlledEmbedder}'s
     * post-acquire, pre-delegate deadline check record onto this embedder's
     * OWN {@code deadlineAbortsTotal} counter, the same one {@link
     * #embedSubBatched}'s between-sub-batch check point already feeds — one
     * counter, two check points, both visible on {@code GET /v1/status}.
     */
    @Override
    public void recordDeadlineAbort() {
        activityTracker.recordDeadlineAbort();
    }

    /**
     * Tokenizes the whole input once (cheap relative to an ONNX forward pass), orders the rows by
     * token length, partitions that order into groups of similar length, and runs one ONNX call
     * per group. Results are scattered back to the caller's input order.
     *
     * <p>Why length-ordered: every group is padded to its longest row, and the cost of a call
     * grows with the padded token count (and, for attention, with its square). Measured on this
     * repo's own chunks (12,999 chunks, mean 278 tokens, 11% at the 512 cap), a request of 16
     * chunks in arrival order padded to 1.62x its real tokens (attention 2.03x); the same chunks
     * grouped by length at {@link #MAX_PAD_WASTE} = 0.25 pad to about 1.12x (attention about
     * 1.22x), in about 4 calls per request instead of one (T2 nexus/index-embedding-throughput-2026-10-08).
     * The local engine is CPU-bound, so those padded tokens are wall time.
     *
     * <p>Two bounds close a group: the padded-token-area ceiling {@link #MAX_PADDED_TOKEN_AREA}
     * (memory, nexus-zu4ma, unchanged) and the padding-waste ceiling. A row is always allowed to
     * start a group, so a single row never fails either bound. The output for a text does not
     * depend on its group beyond float rounding (cosine to the same text alone is above
     * {@code 1 - 1e-6}, pinned by {@link Bge768BatchCompositionTest}).
     *
     * <p>Degrades to the pre-existing single-call behavior when the whole input is already one
     * length: the planner then produces one group (or the area-bounded groups, as before).
     *
     * <p>A deadline abort between groups discards every group already finished: the request
     * returns nothing and the client retries the WHOLE request. A request now has about four
     * groups, so there are more check points per request than before grouping.
     */
    private List<float[]> embedSubBatched(List<String> texts) throws Exception {
        int n = texts.size();
        Encoding[] encodings = new Encoding[n];
        int[] lens = new int[n];
        for (int i = 0; i < n; i++) {
            encodings[i] = tokenizer.encode(texts.get(i));
            // Guard a single empty text against a zero-width group (maxLen >= 1 below).
            lens[i] = Math.min((int) encodings[i].getIds().length, MAX_SEQ_LEN);
        }

        GroupPlan plan = planGroups(lens, MAX_PADDED_TOKEN_AREA, MAX_PAD_WASTE);
        Encoding[] ordered = new Encoding[n];
        for (int k = 0; k < n; k++) ordered[k] = encodings[plan.order()[k]];
        float[][] scattered = new float[n][];

        // Bead nexus-s71lr: this call's own clock, for the "elapsed"/"chunks_per_sec"
        // fields on the progress line below. Independent of progressGate's clock, which
        // is shared instance-wide across every concurrent embed() call.
        long callStartNanos = System.nanoTime();
        int chunksDone = 0;
        int subBatchIndex = 0;
        // nexus-8hdg9 phase 3: the request's cooperative deadline, read ONCE per call
        // (RequestDeadlineProbe.NONE outside a filtered request -> never aborts).
        long deadlineNanos = RequestDeadlineProbe.currentDeadlineNanos();

        int[][] groups = plan.groups();
        for (int[] group : groups) {
            int start = group[0];
            int end = group[1];
            int groupMaxLen = group[2];
            if (groups.length > 1) {
                // Only a request that planned more than one group logs here. Length grouping
                // makes that the usual case for a mixed-length request (about four groups at 16
                // chunks), so this is a debug line per group, not a rare event; a request whose
                // rows all fit one group stays silent.
                log.debug("event=bge768_subbatch start={} size={} maxLen={} totalBatch={}",
                        start, end - start, groupMaxLen, n);
            }
            List<float[]> part = runOnnxSubBatch(ordered, start, end, groupMaxLen);
            for (int k = 0; k < part.size(); k++) scattered[plan.order()[start + k]] = part.get(k);
            chunksDone += end - start;

            long nowNanos = System.nanoTime();
            double elapsedSec = (nowNanos - callStartNanos) / 1_000_000_000.0;
            double chunksPerSec = elapsedSec > 0.0 ? chunksDone / elapsedSec : 0.0;

            // Bead nexus-s71lr, deliverable 2: update the wire-visible activity counters
            // on EVERY sub-batch, unconditionally — GET /v1/status must reflect true
            // current state regardless of how often the log line below is allowed to
            // fire (never throttled the way the log is).
            activityTracker.record(end - start, chunksPerSec, nowNanos);

            // Bead nexus-s71lr: the fix for "the engine logs nothing between per-document
            // upserts" — a structured INFO line per sub-batch, rate-limited (progressGate,
            // shared across every concurrent embed() call on this instance) to about once
            // per PROGRESS_LOG_INTERVAL_NANOS so a large bulk run does not flood INFO.
            if (progressGate.shouldLog(nowNanos)) {
                // code-review-expert pass 2 finding c: queue_depth/thread_width READ from
                // the admission gate (never recomputed), omitted entirely when no gate is
                // wired (every direct test construction of this class) rather than
                // fabricating a 0/absent value.
                LocalOnnxAdmission gate = admissionGate;
                String admissionFields = gate != null
                        ? String.format(" queue_depth=%d thread_width=%d",
                                gate.queueLength(), gate.permits())
                        : "";
                log.info("event=bge768_embed_progress sub_batch={} sub_batch_size={} max_len={} "
                        + "chunks_done={} chunks_total={} elapsed_s={} chunks_per_sec={}{}",
                        subBatchIndex, end - start, groupMaxLen, chunksDone, n,
                        String.format("%.1f", elapsedSec), String.format("%.1f", chunksPerSec),
                        admissionFields);
            }
            subBatchIndex++;

            // A deadline abort discards the groups already finished: nothing is returned
            // for the request, and the client retries the WHOLE request after
            // retry_after_s. With about four groups per request there are more such check
            // points than before length grouping, so more finished work can be thrown away.
            // nexus-8hdg9 phase 3: cooperative deadline check BETWEEN sub-batches, before
            // the next runOnnxSubBatch. Reuses this iteration's nowNanos (design record §4:
            // no second clock read; the added cost is one long comparison). Only when more
            // work remains -- a request whose last sub-batch just completed is never
            // aborted after doing all its work. The admission permit is released by
            // AdmissionControlledEmbedder's finally; the session.run() that just returned
            // is the granularity floor (the deadline never interrupts a run; only
            // shutdown does, through GatedRun's terminate flag, nexus-o5xyx.3).
            if (chunksDone < n && RequestDeadlineProbe.expired(deadlineNanos, nowNanos)) {
                long elapsedMs = (nowNanos - callStartNanos) / 1_000_000L;
                long pastDeadlineMs = (nowNanos - deadlineNanos) / 1_000_000L;
                activityTracker.recordDeadlineAbort();  // GET /v1/status deadline_aborts_total
                log.warn("event=embed_deadline_exceeded embedder=bge768 chunks_done={} chunks_total={} "
                        + "sub_batches_done={} elapsed_ms={} past_deadline_ms={} retry_after_s={}",
                        chunksDone, n, subBatchIndex, elapsedMs, pastDeadlineMs,
                        RequestDeadlineExceededException.DEFAULT_RETRY_AFTER_SECONDS);
                throw new RequestDeadlineExceededException(
                        "embed deadline exceeded after " + chunksDone + "/" + n + " chunks ("
                                + subBatchIndex + " sub-batches, " + elapsedMs + "ms elapsed, "
                                + pastDeadlineMs + "ms past deadline)",
                        RequestDeadlineExceededException.DEFAULT_RETRY_AFTER_SECONDS);
            }
        }
        return new ArrayList<>(java.util.Arrays.asList(scattered));
    }

    /**
     * One partition of a request: {@code order[k]} is the input index of the k-th row in length
     * order, and each {@code groups[g]} is {@code {start, end, maxLen}} over that order
     * ({@code end} exclusive, {@code maxLen >= 1} the group's padded width).
     */
    record GroupPlan(int[] order, int[][] groups) {}

    /**
     * Partition {@code lens} (token counts, already capped at {@link #MAX_SEQ_LEN}) into ONNX
     * groups: stable ascending sort by length, then greedy extension of the current group while
     * BOTH bounds hold: the padded-token area {@code size * maxLen^2 <= maxArea}, and the padding
     * waste {@code size * maxLen <= (1 + maxWaste) * (real tokens in the group)}. The first row of
     * a group is always accepted. Package-private and pure so the planner is testable without the
     * 416 MB model.
     */
    static GroupPlan planGroups(int[] lens, long maxArea, double maxWaste) {
        int n = lens.length;
        Integer[] boxed = new Integer[n];
        for (int i = 0; i < n; i++) boxed[i] = i;
        java.util.Arrays.sort(boxed, java.util.Comparator.comparingInt(i -> lens[i]));  // stable
        int[] order = new int[n];
        for (int k = 0; k < n; k++) order[k] = boxed[k];

        List<int[]> groups = new ArrayList<>();
        int start = 0;
        while (start < n) {
            int end = start + 1;
            long realTokens = Math.max(lens[order[start]], 1);
            int groupMaxLen = (int) realTokens;
            while (end < n) {
                int len = Math.max(lens[order[end]], 1);  // ascending: the new row is the new max
                long size = end - start + 1;
                if (size * len * len > maxArea) break;
                if (size * len > (1.0 + maxWaste) * (realTokens + len)) break;
                realTokens += len;
                groupMaxLen = len;
                end++;
            }
            groups.add(new int[]{start, end, groupMaxLen});
            start = end;
        }
        return new GroupPlan(order, groups.toArray(new int[0][]));
    }

    /**
     * Runs ONE {@code session.run()} over {@code encodings[start, end)}, padded to
     * the caller-supplied {@code maxLen}. This is the sole ONNX invocation point —
     * {@link #onnxInvocationCount} is incremented here so the sub-batch planner's
     * non-vacuity test can prove an oversize request actually produced multiple
     * ONNX calls (nexus-zu4ma).
     */
    private List<float[]> runOnnxSubBatch(Encoding[] encodings, int start, int end, int maxLen) throws Exception {
        int batchSize = end - start;
        maxLen = Math.max(maxLen, 1);

        long[] inputIdsFlat      = new long[batchSize * maxLen];
        long[] attentionMaskFlat = new long[batchSize * maxLen];

        for (int i = 0; i < batchSize; i++) {
            long[] ids  = encodings[start + i].getIds();
            long[] mask = encodings[start + i].getAttentionMask();
            int seqLen  = Math.min(ids.length, maxLen);
            int offset  = i * maxLen;
            for (int j = 0; j < seqLen; j++) {
                inputIdsFlat[offset + j]      = ids[j];
                attentionMaskFlat[offset + j] = mask[j];
                // tokenTypeIdsFlat stays 0 — already zero-initialised
            }
            // padding positions: 0 ids, 0 mask
        }

        long[] shape = {batchSize, maxLen};

        // nexus-o5xyx.3: inference logs through ORT's logging manager too (ExecuteKernel's
        // Capture), so the run sits in the OrtInitGate from tensor creation to tensor
        // release and shutdown cancels it (see GatedRun). Throws ShutdownInProgressException
        // once exit has begun, so a multi-sub-batch request stops at the next sub-batch.
        try (GatedRun run = GatedRun.open("bge768-run")) {
            OnnxTensor inputIdsTensor      = null;
            OnnxTensor attentionMaskTensor = null;
            OnnxTensor tokenTypeIdsTensor  = null;
            try {
                inputIdsTensor      = OnnxTensor.createTensor(ortEnv, LongBuffer.wrap(inputIdsFlat), shape);
                attentionMaskTensor = OnnxTensor.createTensor(ortEnv, LongBuffer.wrap(attentionMaskFlat), shape);

                Map<String, OnnxTensor> inputs = new HashMap<>();
                inputs.put("input_ids", inputIdsTensor);
                inputs.put("attention_mask", attentionMaskTensor);
                if (wantsTokenTypeIds) {
                    long[] tokenTypeIdsFlat = new long[batchSize * maxLen]; // all zeros
                    tokenTypeIdsTensor = OnnxTensor.createTensor(ortEnv, LongBuffer.wrap(tokenTypeIdsFlat), shape);
                    inputs.put("token_type_ids", tokenTypeIdsTensor);
                }

                onnxInvocationCount.incrementAndGet();
                try (OrtSession.Result result = session.run(inputs, run.options())) {
                    // Output 0 = last_hidden_state shape [batch, seq, 768]
                    float[][][] hiddenState = (float[][][]) result.get(0).getValue();

                    List<float[]> embeddings = new ArrayList<>(batchSize);
                    for (int i = 0; i < batchSize; i++) {
                        embeddings.add(clsPoolNormalize(hiddenState[i]));
                    }
                    return embeddings;
                } catch (OrtException e) {
                    throw run.cancelledOr(e);
                }
            } finally {
                if (inputIdsTensor != null)      inputIdsTensor.close();
                if (attentionMaskTensor != null) attentionMaskTensor.close();
                if (tokenTypeIdsTensor != null)  tokenTypeIdsTensor.close();
            }
        }
    }

    /** Test-only: number of {@code session.run()} calls since construction or the
     * last {@link #resetOnnxInvocationCount()}. Package-private (see
     * {@link #onnxInvocationCount}'s javadoc). */
    int onnxInvocationCount() {
        return onnxInvocationCount.get();
    }

    /** Test-only: zero the invocation counter. Package-private (see
     * {@link #onnxInvocationCount}'s javadoc). */
    void resetOnnxInvocationCount() {
        onnxInvocationCount.set(0);
    }

    /**
     * CLS pooling + L2-normalize a single sequence.
     *
     * <p>bge-base-en-v1.5 uses the {@code [CLS]} token representation (row 0 of
     * {@code last_hidden_state}) as the sentence embedding — NOT mean-pooling.
     * Confirmed against fastembed: CLS+norm scores cosine 1.0; mean+norm scores
     * 0.825 (RDR-160 CA-2).
     *
     * @param hidden shape [seq, 768] — one row per token; row 0 is {@code [CLS]}
     */
    // Package-private (not private) so the pooling math has a model-free unit-test
    // guard on CI, where the 416MB ONNX is absent and the parity gate is skipped
    // (RDR-160 P1 review S1). See Bge768EmbedderMathTest.
    static float[] clsPoolNormalize(float[][] hidden) {
        float[] cls = hidden[0].clone();   // [CLS] token

        double sumSq = 0.0;
        for (float v : cls) sumSq += (double) v * v;
        float norm = (float) Math.sqrt(sumSq);
        if (norm > 1e-12f) {
            for (int d = 0; d < cls.length; d++) cls[d] /= norm;
        }
        return cls;
    }

    @Override
    public void close() {
        try { session.close();   } catch (Exception ignored) {}
        try { tokenizer.close(); } catch (Exception ignored) {}
        // ortEnv is a JVM-level singleton; do not close it
    }
}
