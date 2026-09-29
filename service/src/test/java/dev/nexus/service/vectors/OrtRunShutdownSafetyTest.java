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
 * nexus-o5xyx.3 — a SIGTERM that lands while {@code session.run()} is in flight
 * must end in a clean exit, never a native crash in {@code onnxruntime::logging}
 * under {@code InferenceSession::Run}.
 *
 * <p>Spawns {@link OrtRunShutdownProbe} (gate installed, real {@link Bge768Embedder},
 * {@value #THREADS} threads embedding in a loop) and sends SIGTERM at a sweep of
 * delays after the loop is hot. The crash is detected by its {@code hs_err} file,
 * not only the exit status: a crash during exit can leave status 143 (measured,
 * ungated: 3 crash reports in 40 runs, every status 143).
 *
 * <p>Crash detection alone is statistical. With the init-only gate, 5 of 100 signals
 * crashed (macOS arm64, 8 threads; 7 of 80 ungated), so {@value #ITERATIONS}
 * iterations see a crash from a regression back to that gate with probability about
 * 1 - 0.95^30 = 0.79. The deterministic checks are the other two below.
 *
 * <p>Cancellation must be observed: the gate must log that it cancelled live runs on at
 * least {@value #MIN_CANCELLED} iterations. That fails on any host when the canceller
 * is gone (measured: removing its registration turns this test red), and it proves the
 * signals met live runs. The drain must also never expire its bound: an expiry means
 * exit went ahead with native work in flight, the crash window whether or not this run
 * happened to crash. Model gating follows {@link OrtTestModel} (skip only when nothing
 * is provisioned).
 */
class OrtRunShutdownSafetyTest {

    private static final int ITERATIONS = 30;
    private static final int THREADS = 8;
    private static final int MIN_CANCELLED = ITERATIONS / 2;
    private static final String CANCEL_LOG = "event=ort_run_cancelled count=";
    private static final String TIMEOUT_LOG = "event=ort_init_shutdown_wait_timeout";

    private record Outcome(int delayMs, int exitCode, boolean cancelled, boolean timedOut, String output) {}

    @Test
    @Timeout(value = 10, unit = TimeUnit.MINUTES)
    void sigtermDuringInference_exitsCleanlyNeverCrashes(@TempDir Path work) throws Exception {
        Assumptions.assumeTrue(!System.getProperty("os.name", "").toLowerCase().contains("win"),
                "SIGTERM semantics are POSIX-only");
        OrtTestModel.requireBgeOrSkip();

        List<Outcome> outcomes = new ArrayList<>();
        for (int i = 0; i < ITERATIONS; i++) {
            // Deterministic sweep across 200..1360 ms after the loop is hot.
            outcomes.add(runOnce(work, 200 + 40 * i));
        }

        for (Outcome o : outcomes) {
            assertThat(o.exitCode())
                    .as("SIGTERM %d ms into sustained inference: exit must be 143, not a "
                            + "signal crash. child output:%n%s", o.delayMs(), o.output())
                    .isEqualTo(143);
        }
        try (Stream<Path> files = Files.list(work)) {
            assertThat(files.map(p -> p.getFileName().toString()).filter(n -> n.startsWith("hs_err")))
                    .as("no JVM crash report may be written (a crash during exit can still exit 143)")
                    .isEmpty();
        }
        assertThat(outcomes.stream().filter(Outcome::timedOut).map(Outcome::delayMs))
                .as("the drain must finish inside its bound: cancelled runs return at a kernel "
                        + "boundary, so an expiry means exit proceeded with inference in flight")
                .isEmpty();
        long cancelled = outcomes.stream().filter(Outcome::cancelled).count();
        assertThat(cancelled)
                .as("non-vacuity: the gate must have cancelled live runs on at least "
                        + MIN_CANCELLED + " of " + ITERATIONS + " iterations")
                .isGreaterThanOrEqualTo(MIN_CANCELLED);
    }

    private static Outcome runOnce(Path work, int delayMs) throws IOException, InterruptedException {
        Path java = Path.of(System.getProperty("java.home"), "bin", "java");
        Process p = new ProcessBuilder(
                java.toString(),
                "-XX:ErrorFile=" + work.resolve("hs_err_%p.log"),
                "-cp", System.getProperty("java.class.path"),
                OrtRunShutdownProbe.class.getName(),
                Integer.toString(THREADS))
                .directory(work.toFile())
                .redirectErrorStream(true)
                .start();

        List<String> lines = new CopyOnWriteArrayList<>();
        CountDownLatch hot = new CountDownLatch(1);
        Thread reader = new Thread(() -> {
            try (var r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = r.readLine()) != null) {
                    lines.add(line);
                    if (line.equals("RUN_BEGIN")) hot.countDown();
                }
            } catch (IOException ignored) {
                // process torn down mid-read
            }
        }, "run-probe-reader");
        reader.setDaemon(true);
        reader.start();

        if (!hot.await(60, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            throw new AssertionError("probe never reached RUN_BEGIN; output: " + lines);
        }
        Thread.sleep(delayMs);
        OrtTestProcesses.sigterm(p);

        if (!p.waitFor(60, TimeUnit.SECONDS)) {
            p.destroyForcibly();
            throw new AssertionError("probe did not exit within 60s of SIGTERM at +" + delayMs
                    + "ms; output: " + lines);
        }
        reader.join(5_000);
        boolean cancelled = lines.stream().anyMatch(l -> l.contains(CANCEL_LOG));
        boolean timedOut = lines.stream().anyMatch(l -> l.contains(TIMEOUT_LOG));
        return new Outcome(delayMs, p.exitValue(), cancelled, timedOut, String.join("\n", lines));
    }
}
