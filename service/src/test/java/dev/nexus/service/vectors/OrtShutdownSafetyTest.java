// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.io.TempDir;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-o5xyx.1 — a SIGTERM that lands while {@code OrtSession} creation is in
 * flight on the main thread must end in a clean exit (143), never a native
 * crash (134, SIGSEGV then abort inside {@code onnxruntime::logging}).
 *
 * <p>The crash is native and timing-bound, so an in-JVM assertion cannot see it.
 * This test spawns {@link OrtShutdownProbe} (the real {@link Bge768Embedder} on
 * a main thread, hook installed first as {@code Main} does) and sends SIGTERM at
 * a sweep of delays after {@code INIT_BEGIN}. On the reference machine
 * (hellmini, macOS arm64, ORT 1.20.0) the unguarded process crashes for
 * delays around 300 to 400 ms; the sweep straddles that window so the result
 * does not depend on one lucky offset.
 *
 * <p>Non-vacuity: the sweep must actually land SIGTERM inside init on several
 * runs (the parent sees INIT_DONE only after it sent the signal, or never).
 * The ~416MB model is not committed; without it the test is skipped LOUDLY,
 * exactly like {@code Bge768ParityTest}.
 */
class OrtShutdownSafetyTest {

    /** Millis after INIT_BEGIN at which SIGTERM is sent. Spans class-load to post-load. */
    private static final int[] KILL_DELAYS_MS =
            {0, 100, 200, 250, 300, 350, 400, 450, 500, 600, 800};

    /** Sweep points at which the signal must have landed mid-init, or the sweep proved nothing. */
    private static final int MIN_MID_INIT_KILLS = 3;

    private record Outcome(int delayMs, int exitCode, boolean midInit, String output) {}

    @Test
    @Timeout(value = 5, unit = TimeUnit.MINUTES)
    void sigtermDuringSessionCreation_exitsCleanlyNeverCrashes(@TempDir Path work) throws Exception {
        Assumptions.assumeTrue(!System.getProperty("os.name", "").toLowerCase().contains("win"),
                "SIGTERM semantics are POSIX-only");
        Assumptions.assumeTrue(Files.isRegularFile(Path.of(Bge768Embedder.DEFAULT_MODEL_PATH)),
                "SKIPPED (not passed): bge ONNX model not provisioned at "
                        + Bge768Embedder.DEFAULT_MODEL_PATH + " — nexus-o5xyx.1's crash is only "
                        + "reachable with a real model; provision via `nx init --service`");
        Assumptions.assumeTrue(Files.isRegularFile(Path.of(Bge768Embedder.DEFAULT_TOKENIZER_PATH)),
                "SKIPPED (not passed): bge tokenizer not provisioned");

        List<Outcome> outcomes = new ArrayList<>();
        for (int delay : KILL_DELAYS_MS) {
            outcomes.add(runOnce(work, delay));
        }

        for (Outcome o : outcomes) {
            assertThat(o.exitCode())
                    .as("SIGTERM %d ms after INIT_BEGIN: exit must be 143 (clean SIGTERM exit), "
                            + "not 134 (SIGSEGV then abort in onnxruntime LoggingManager). "
                            + "child output:%n%s", o.delayMs(), o.output())
                    .isEqualTo(143);
        }
        try (Stream<Path> files = Files.list(work)) {
            assertThat(files.map(p -> p.getFileName().toString()).filter(n -> n.startsWith("hs_err")))
                    .as("no JVM crash report may be written")
                    .isEmpty();
        }
        long midInit = outcomes.stream().filter(Outcome::midInit).count();
        assertThat(midInit)
                .as("non-vacuity: SIGTERM must have landed inside session creation on at least "
                        + MIN_MID_INIT_KILLS + " sweep points (outcomes: %s)", outcomes)
                .isGreaterThanOrEqualTo(MIN_MID_INIT_KILLS);
    }

    private static Outcome runOnce(Path work, int delayMs) throws IOException, InterruptedException {
        Path java = Path.of(System.getProperty("java.home"), "bin", "java");
        ProcessBuilder pb = new ProcessBuilder(
                java.toString(),
                "-XX:ErrorFile=" + work.resolve("hs_err_%p.log"),
                "-cp", System.getProperty("java.class.path"),
                OrtShutdownProbe.class.getName())
                .directory(work.toFile())
                .redirectErrorStream(true);
        Process p = pb.start();

        List<String> lines = new CopyOnWriteArrayList<>();
        CountDownLatch begun = new CountDownLatch(1);
        long[] doneAtNanos = {0L};
        Thread reader = new Thread(() -> {
            try (var r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = r.readLine()) != null) {
                    lines.add(line);
                    if (line.equals("INIT_BEGIN")) begun.countDown();
                    if (line.equals("INIT_DONE") || line.equals("INIT_REFUSED")) {
                        synchronized (doneAtNanos) { doneAtNanos[0] = System.nanoTime(); }
                    }
                }
            } catch (IOException ignored) {
                // process torn down mid-read
            }
        }, "probe-reader");
        reader.setDaemon(true);
        reader.start();

        if (!begun.await(60, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            throw new AssertionError("probe never reached INIT_BEGIN; output: " + lines);
        }
        if (delayMs > 0) Thread.sleep(delayMs);
        long killedAt = System.nanoTime();
        p.destroy(); // SIGTERM

        if (!p.waitFor(60, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            throw new AssertionError("probe did not exit within 60s of SIGTERM at +" + delayMs
                    + "ms; output: " + lines);
        }
        reader.join(5_000);
        long done;
        synchronized (doneAtNanos) { done = doneAtNanos[0]; }
        boolean midInit = done == 0L || done > killedAt;
        return new Outcome(delayMs, p.exitValue(), midInit, String.join("\n", lines));
    }
}
