// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.djl.huggingface.tokenizers.Encoding;
import ai.djl.huggingface.tokenizers.HuggingFaceTokenizer;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantReadWriteLock;
import java.util.function.Supplier;

/**
 * nexus-89lp5 -- the one holder of a native HuggingFace tokenizer ({@link Bge768Embedder},
 * {@link OnnxEmbedder}, {@link CrossEncoderReranker} each own one), and the reason the engine no longer
 * aborts when SIGTERM lands under embed load.
 *
 * <p><b>The defect.</b> {@link GatedRun} covers {@code session.run()} only. {@code tokenizer.encode} ran
 * outside the {@link OrtInitGate}, so {@link OrtInitGate#quiesce} counted a request that was tokenizing as
 * nothing in flight. Cancelling the live runs released their admission permits, requests queued behind them
 * were admitted and began to tokenize, the drain saw zero in flight and returned, and {@code System.exit}
 * started Main's shutdown hook, whose {@code embedder.close()} frees the native tokenizer under those
 * encodes: DJL's {@code Native resource has been released already} on the lucky side of the race, a native
 * abort (exit 134, not 143) on the other. Seen once in the linux-amd64 native smoke leg of
 * engine-service-release dispatch 41383140795; the close order in Main is the same on every platform and in
 * the JVM.
 *
 * <p><b>The fix has two parts, each enough for one exit path.</b>
 * <ol>
 *   <li>Every encode is an {@link OrtInitGate} scope without a canceller (encodes are milliseconds, so they
 *       are waited for, not cancelled). {@code quiesce} therefore waits for them, and an encode that starts
 *       after shutdown began is refused with {@link OrtInitGate.ShutdownInProgressException}, the same
 *       refusal a new run gets, which the HTTP layer answers as a retryable 503. After a drain that returned
 *       true no encode is live and none can start, so the hook's close is safe. When the bound expires the
 *       process halts without a hook and the tokenizer is never closed.</li>
 *   <li>{@link #close()} itself excludes encodes: it marks the tokenizer closing (new encodes are refused),
 *       waits, bounded, for the encodes already inside to return, and only then frees the native handle.
 *       That holds on the exit paths the gate does not cover ({@code System.exit} from application code, a
 *       test or a double close), so "never closed under an in-flight encode" is a property of this class and
 *       not of the shutdown order. If the wait expires the handle is left for process exit.</li>
 * </ol>
 */
public final class GatedTokenizer implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(GatedTokenizer.class);

    /** How long {@link #close()} waits for encodes already inside; under the supervisor's 5 s SIGKILL grace. */
    static final long CLOSE_WAIT_MILLIS = 2_000L;

    /** What this class guards: test seam over the native tokenizer. */
    interface Codec {
        Encoding encode(String text);

        Encoding encode(String text, String pair);

        void close();
    }

    private final OrtInitGate gate;
    private final Codec codec;
    private final long closeWaitMillis;
    /** Readers are encodes in progress; the writer is the close. */
    private final ReentrantReadWriteLock lock = new ReentrantReadWriteLock();
    private volatile boolean closing;

    /**
     * A truncating, non-padding tokenizer loaded from {@code tokenizerPath}, guarded by the process
     * gate. Built through {@link LocalTokenizers} (nexus-17jvq).
     */
    public static GatedTokenizer build(String tokenizerPath, int maxLength) throws IOException {
        HuggingFaceTokenizer hf = LocalTokenizers.build(tokenizerPath, maxLength);
        return new GatedTokenizer(OrtInitGate.process(), new Codec() {
            @Override public Encoding encode(String text) { return hf.encode(text); }
            @Override public Encoding encode(String text, String pair) { return hf.encode(text, pair); }
            @Override public void close() { hf.close(); }
        }, CLOSE_WAIT_MILLIS);
    }

    GatedTokenizer(OrtInitGate gate, Codec codec, long closeWaitMillis) {
        this.gate = gate;
        this.codec = codec;
        this.closeWaitMillis = closeWaitMillis;
    }

    /**
     * @throws OrtInitGate.ShutdownInProgressException if shutdown has begun or this tokenizer is closed;
     *         nothing native was touched
     */
    public Encoding encode(String text) {
        return guarded(() -> codec.encode(text));
    }

    /** Pair encoding, {@code [CLS] text [SEP] pair [SEP]}; refusal as {@link #encode(String)}. */
    public Encoding encode(String text, String pair) {
        return guarded(() -> codec.encode(text, pair));
    }

    private Encoding guarded(Supplier<Encoding> body) {
        OrtInitGate.Scope scope = gate.enter("tokenize");
        try {
            // Checked before the lock too: a close waiting for the encodes inside queues as the writer,
            // and a blocking read lock would park a late encode behind it for the whole close wait.
            // tryLock() does not queue behind a waiting writer; it fails only while the close holds it.
            if (closing || !lock.readLock().tryLock()) {
                throw closed();
            }
            try {
                if (closing) {
                    throw closed();
                }
                return body.get();
            } finally {
                lock.readLock().unlock();
            }
        } finally {
            scope.close();
        }
    }

    private static OrtInitGate.ShutdownInProgressException closed() {
        return new OrtInitGate.ShutdownInProgressException(
                "refusing to tokenize: tokenizer closed, shutdown in progress");
    }

    /**
     * Refuse new encodes, wait for those inside, then free the native handle. Idempotent. Never throws.
     */
    @Override
    public void close() {
        closing = true;
        try {
            if (!lock.writeLock().tryLock(closeWaitMillis, TimeUnit.MILLISECONDS)) {
                log.warn("event=tokenizer_close_skipped reason=encode_in_flight bound_ms={}", closeWaitMillis);
                return;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.warn("event=tokenizer_close_skipped reason=interrupted");
            return;
        }
        try {
            codec.close();
        } catch (RuntimeException ignored) {
            // a second close, or a handle already gone: nothing left to free
        } finally {
            lock.writeLock().unlock();
        }
    }
}
