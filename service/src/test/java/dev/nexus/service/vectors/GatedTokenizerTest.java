// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ai.djl.huggingface.tokenizers.Encoding;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-89lp5 — a SIGTERM under embed load must never free the native tokenizer under an
 * {@code encode} in flight, and an {@code encode} that starts after shutdown began must be
 * refused cleanly. The tokenizer stand-in blocks INSIDE {@code encode} on a latch, which is
 * the window the real native encode has; private gates throughout, the process gate is never
 * closed here.
 */
@Timeout(value = 60, unit = TimeUnit.SECONDS)
class GatedTokenizerTest {

    /** Blocks inside {@code encode} until released; records the order of encode-end and close. */
    private static final class BlockingCodec implements GatedTokenizer.Codec {
        final CountDownLatch inEncode = new CountDownLatch(1);
        final CountDownLatch release = new CountDownLatch(1);
        final List<String> events = new CopyOnWriteArrayList<>();
        final AtomicInteger encodeCalls = new AtomicInteger();
        final boolean blocking;

        BlockingCodec(boolean blocking) { this.blocking = blocking; }

        @Override public Encoding encode(String text) {
            encodeCalls.incrementAndGet();
            if (blocking) {
                inEncode.countDown();
                try {
                    release.await();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            events.add("encode-end");
            return null;
        }

        @Override public Encoding encode(String text, String pair) { return encode(text); }

        @Override public void close() { events.add("close"); }
    }

    private static final ExecutorService POOL = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "tok-test");
        t.setDaemon(true);
        return t;
    });

    /** Start an encode that blocks inside the codec; returns once it is inside. */
    private static Future<?> blockedEncode(GatedTokenizer tok, BlockingCodec codec) throws Exception {
        Future<?> f = POOL.submit(() -> tok.encode("text"));
        assertThat(codec.inEncode.await(10, TimeUnit.SECONDS)).as("encode reached the codec").isTrue();
        return f;
    }

    @Test
    void closeWaitsForAnEncodeInFlightThenFreesTheHandle() throws Exception {
        BlockingCodec codec = new BlockingCodec(true);
        GatedTokenizer tok = new GatedTokenizer(new OrtInitGate(), codec, 10_000);
        Future<?> encode = blockedEncode(tok, codec);

        Future<?> close = POOL.submit(tok::close);
        Thread.sleep(300);   // a close that does not wait would have freed the handle by now
        assertThat(codec.events).as("close must not free the tokenizer under a running encode").isEmpty();

        codec.release.countDown();
        encode.get(10, TimeUnit.SECONDS);
        close.get(10, TimeUnit.SECONDS);
        assertThat(codec.events).containsExactly("encode-end", "close");
    }

    @Test
    void anEncodeThatStartsOnceCloseHasBegunIsRefusedAndTouchesNothing() throws Exception {
        BlockingCodec codec = new BlockingCodec(true);
        GatedTokenizer tok = new GatedTokenizer(new OrtInitGate(), codec, 10_000);
        Future<?> encode = blockedEncode(tok, codec);
        Future<?> close = POOL.submit(tok::close);
        Thread.sleep(300);   // close is now waiting for the encode inside

        int before = codec.encodeCalls.get();
        // Answered at once: a refusal parked behind the waiting close would sit out its whole bound.
        for (Future<?> late : List.of(POOL.submit(() -> tok.encode("late")),
                                      POOL.submit(() -> tok.encode("late", "pair")))) {
            assertThatThrownBy(() -> late.get(2, TimeUnit.SECONDS))
                    .hasCauseInstanceOf(OrtInitGate.ShutdownInProgressException.class);
        }
        assertThat(codec.encodeCalls.get()).as("a refused encode never reaches the native tokenizer")
                .isEqualTo(before);

        codec.release.countDown();
        encode.get(10, TimeUnit.SECONDS);
        close.get(10, TimeUnit.SECONDS);
    }

    @Test
    void anEncodeAfterTheGateClosedIsRefusedAsACleanShutdownRefusal() {
        OrtInitGate gate = new OrtInitGate();
        BlockingCodec codec = new BlockingCodec(false);
        GatedTokenizer tok = new GatedTokenizer(gate, codec, 10_000);
        assertThat(gate.quiesce(0)).isTrue();   // shutdown has begun, nothing in flight

        assertThatThrownBy(() -> tok.encode("late"))
                .isInstanceOf(OrtInitGate.ShutdownInProgressException.class)
                .hasMessageContaining("tokenize");
        assertThatThrownBy(() -> tok.encode("late", "pair"))
                .isInstanceOf(OrtInitGate.ShutdownInProgressException.class);
        assertThat(codec.encodeCalls.get()).isZero();
        assertThat(gate.quiesce(0)).as("a refused encode leaves nothing in flight").isTrue();
    }

    @Test
    void theDrainWaitsForAnEncodeInFlight() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        BlockingCodec codec = new BlockingCodec(true);
        GatedTokenizer tok = new GatedTokenizer(gate, codec, 10_000);
        Future<?> encode = blockedEncode(tok, codec);

        assertThat(gate.quiesce(100))
                .as("an encode is native work in flight: the drain must not report it quiet "
                        + "(this is what let System.exit start the hook under it)")
                .isFalse();

        codec.release.countDown();
        encode.get(10, TimeUnit.SECONDS);
        assertThat(gate.quiesce(2_000)).isTrue();
    }

    /**
     * The production order, end to end: a signal's deferred-exit thread drains the gate, then
     * the shutdown hook closes the tokenizer. The close must see the encode finished.
     */
    @Test
    void drainThenCloseNeverFreesTheTokenizerUnderAnEncode() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        BlockingCodec codec = new BlockingCodec(true);
        GatedTokenizer tok = new GatedTokenizer(gate, codec, 10_000);
        Future<?> encode = blockedEncode(tok, codec);

        Future<Boolean> exit = POOL.submit(() -> {
            boolean drained = gate.quiesce(10_000);   // the deferred-exit thread
            tok.close();                               // Main's shutdown hook, via embedder.close()
            return drained;
        });
        Thread.sleep(300);
        assertThat(codec.events).as("neither the drain nor the close may finish under the encode").isEmpty();

        codec.release.countDown();
        encode.get(10, TimeUnit.SECONDS);
        assertThat(exit.get(10, TimeUnit.SECONDS)).isTrue();
        assertThat(codec.events).containsExactly("encode-end", "close");
    }

    @Test
    void closeLeavesTheHandleForProcessExitWhenAnEncodeOutlastsItsBound() throws Exception {
        BlockingCodec codec = new BlockingCodec(true);
        GatedTokenizer tok = new GatedTokenizer(new OrtInitGate(), codec, 50);
        Future<?> encode = blockedEncode(tok, codec);

        tok.close();   // returns after the 50 ms bound
        assertThat(codec.events).as("a close that timed out must not free the handle").isEmpty();

        codec.release.countDown();
        encode.get(10, TimeUnit.SECONDS);
        assertThat(codec.events).containsExactly("encode-end");
    }

    @Test
    void closeIsIdempotentAndEncodeAfterCloseIsRefused() {
        BlockingCodec codec = new BlockingCodec(false);
        GatedTokenizer tok = new GatedTokenizer(new OrtInitGate(), codec, 1_000);
        tok.encode("a");
        tok.close();
        tok.close();
        assertThatThrownBy(() -> tok.encode("b")).isInstanceOf(OrtInitGate.ShutdownInProgressException.class);
        assertThat(codec.events).containsExactly("encode-end", "close", "close");
    }

    // ── The real native tokenizer ─────────────────────────────────────────────

    private static final String TINY_TOKENIZER =
        "{\"version\":\"1.0\",\"truncation\":null,\"padding\":null,\"added_tokens\":[],"
        + "\"normalizer\":null,\"pre_tokenizer\":{\"type\":\"Whitespace\"},\"post_processor\":null,"
        + "\"decoder\":null,\"model\":{\"type\":\"WordLevel\",\"vocab\":{\"[UNK]\":0,\"hello\":1},"
        + "\"unk_token\":\"[UNK]\"}}";

    /**
     * Eight threads encode on a real DJL tokenizer in a tight loop while it is closed, 40 times.
     * Every thread must end in an Encoding or the clean refusal, never DJL's {@code Native
     * resource has been released already} (an IllegalStateException) or a native abort. Measured
     * on the raw tokenizer the same loop hits that exception; see the bead for the revert result.
     */
    @Test
    void closingARealTokenizerUnderEncodeLoadOnlyEverRefusesCleanly(@TempDir Path dir) throws Exception {
        Path file = dir.resolve("tokenizer.json");
        Files.writeString(file, TINY_TOKENIZER);
        List<Throwable> unexpected = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger encoded = new AtomicInteger();
        AtomicInteger refused = new AtomicInteger();

        for (int round = 0; round < 40; round++) {
            GatedTokenizer tok = GatedTokenizer.build(file.toString(), 16);
            CountDownLatch hot = new CountDownLatch(8);
            List<Future<?>> workers = new ArrayList<>();
            for (int t = 0; t < 8; t++) {
                workers.add(POOL.submit(() -> {
                    boolean announced = false;
                    while (true) {
                        try {
                            tok.encode("hello hello hello hello hello");
                            encoded.incrementAndGet();
                            if (!announced) { hot.countDown(); announced = true; }
                        } catch (OrtInitGate.ShutdownInProgressException e) {
                            refused.incrementAndGet();
                            if (!announced) hot.countDown();
                            return;
                        } catch (Throwable e) {
                            unexpected.add(e);
                            if (!announced) hot.countDown();
                            return;
                        }
                    }
                }));
            }
            assertThat(hot.await(10, TimeUnit.SECONDS)).isTrue();
            tok.close();
            for (Future<?> w : workers) w.get(10, TimeUnit.SECONDS);
        }
        assertThat(unexpected).as("an encode under a close must be refused cleanly, never an ISE").isEmpty();
        assertThat(encoded.get()).as("non-vacuity: encodes ran before each close").isGreaterThanOrEqualTo(40 * 8);
        assertThat(refused.get()).as("non-vacuity: every worker met the refusal").isEqualTo(40 * 8);
    }

    // ── Every native tokenizer holder goes through the guard ──────────────────

    /**
     * The holders are Bge768Embedder, OnnxEmbedder and CrossEncoderReranker today; this census is
     * what makes a fourth (or a revert of one of these) fail, instead of leaving an unguarded
     * encode outside the gate. Only the two classes that own the native type may name it.
     */
    @Test
    void noMainClassOtherThanTheGuardAndItsBuilderNamesTheNativeTokenizer() throws Exception {
        Path root = Path.of("src", "main", "java");
        List<String> offenders = new ArrayList<>();
        int scanned = 0;
        try (Stream<Path> files = Files.walk(root)) {
            for (Path p : (Iterable<Path>) files.filter(f -> f.toString().endsWith(".java"))::iterator) {
                scanned++;
                String name = p.getFileName().toString();
                if (name.equals("GatedTokenizer.java") || name.equals("LocalTokenizers.java")) continue;
                if (Files.readString(p).contains("HuggingFaceTokenizer")) offenders.add(name);
            }
        }
        assertThat(scanned).as("non-vacuity: the census must have walked the main tree").isGreaterThan(100);
        assertThat(offenders)
                .as("a class holding a raw HuggingFaceTokenizer encodes outside the OrtInitGate (nexus-89lp5)")
                .isEmpty();
        for (String holder : List.of("Bge768Embedder.java", "OnnxEmbedder.java", "CrossEncoderReranker.java")) {
            assertThat(Files.readString(root.resolve("dev/nexus/service/vectors/" + holder)))
                    .as("%s builds its tokenizer through the guard", holder)
                    .contains("GatedTokenizer.build(");
        }
    }
}
