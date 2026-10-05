// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-o5xyx.1 — the REAL {@code sun.misc.Signal} path, without a model, so it
 * runs in CI where the 416MB bge ONNX is absent ({@link OrtShutdownSafetyTest}
 * skips there). A child JVM installs the real handlers, holds a gate scope for
 * {@value #HOLD_MS} ms, and receives SIGTERM after {@value #KILL_AFTER_MS} ms.
 * Proves, with real signals: exit is deferred until the scope is released
 * ({@code RELEASED} is printed before exit), the status is 143 (what the JVM's
 * own default handler produces), and the deferral is logged.
 *
 * <p>Does not prove the ORT crash itself is gone; that needs the model.
 */
class OrtInitGateSignalTest {

    private static final long HOLD_MS = 1_500;
    private static final long KILL_AFTER_MS = 100;

    private record Run(int exit, List<String> lines, long exitedAfterMs) {
        String out() {
            return String.join("\n", lines);
        }
    }

    /**
     * Starts the probe, waits for {@code HOLDING}, then signals it: through {@code trigger} when given
     * (an external {@code kill -TERM}, POSIX only), otherwise the probe was started with
     * {@code raise-term} and signals itself.
     */
    private static Run runProbe(String[] extraArgs, java.util.function.Consumer<Process> trigger) throws Exception {
        List<String> cmd = new java.util.ArrayList<>(List.of(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", System.getProperty("java.class.path"),
                OrtGateHoldProbe.class.getName(), Long.toString(HOLD_MS)));
        cmd.addAll(List.of(extraArgs));
        Process p = new ProcessBuilder(cmd).redirectErrorStream(true).start();
        List<String> lines = new CopyOnWriteArrayList<>();
        CountDownLatch holding = new CountDownLatch(1);
        Thread reader = new Thread(() -> {
            try (var r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = r.readLine()) != null) {
                    lines.add(line);
                    if (line.equals("HOLDING")) holding.countDown();
                }
            } catch (IOException ignored) {
                // torn down mid-read
            }
        }, "hold-probe-reader");
        reader.setDaemon(true);
        reader.start();

        assertThat(holding.await(60, TimeUnit.SECONDS)).as("probe reached HOLDING; output: %s", lines).isTrue();
        long signalledAt = System.nanoTime();
        if (trigger != null) {
            Thread.sleep(KILL_AFTER_MS);
            signalledAt = System.nanoTime();
            trigger.accept(p);
        }
        assertThat(p.waitFor(60, TimeUnit.SECONDS)).as("probe exits after SIGTERM; output: %s", lines).isTrue();
        long exitedAfterMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - signalledAt);
        reader.join(5_000);
        return new Run(p.exitValue(), lines, exitedAfterMs);
    }

    @Test
    @Timeout(value = 2, unit = TimeUnit.MINUTES)
    void realSigtermDuringAHeldScopeDefersExitThenExits143() throws Exception {
        Assumptions.assumeTrue(!WindowsHost.isWindows(System.getProperty("os.name")),
                "an external kill -TERM is POSIX-only; the Windows arm is selfRaisedSigterm...");

        Run run = runProbe(new String[0], p -> {
            try {
                OrtTestProcesses.sigterm(p); // not destroy(): that closes the streams we still need to read
            } catch (IOException | InterruptedException e) {
                throw new IllegalStateException(e);
            }
        });

        String out = run.out();
        assertThat(run.exit()).as("exit status is the default handler's 128+15; output:%n%s", out).isEqualTo(143);
        assertThat(run.lines())
                .as("exit must wait for the held scope: RELEASED is printed before the process ends")
                .contains("RELEASED");
        assertThat(run.exitedAfterMs())
                .as("exit no earlier than the hold (%d ms minus the %d ms already elapsed)", HOLD_MS, KILL_AFTER_MS)
                .isGreaterThanOrEqualTo(HOLD_MS - KILL_AFTER_MS - 300);
        assertThat(out).as("the deferral is logged").contains("ort_init_shutdown_wait in_flight=1");
        assertThat(out).as("real handlers installed").doesNotContain("ort_init_signal_gate_unavailable");
    }

    /**
     * The Windows arm, and the same real {@code sun.misc.Signal} path everywhere else: there is no
     * {@code kill} on Windows, so the probe raises TERM on itself. Exit is deferred, the status is
     * 128+15, TERM and INT install. {@code BREAK} installs only in a native image; a JVM refuses it
     * ({@code Signal already used by VM or OS: SIGBREAK}), so it is allowed to be unavailable on
     * Windows and nowhere else. Not skipped on any host, so this cannot pass vacuously by skipping;
     * on Windows it is the only real-signal evidence this class carries.
     */
    @Test
    @Timeout(value = 2, unit = TimeUnit.MINUTES)
    void selfRaisedSigtermDuringAHeldScopeDefersExitThenExits143() throws Exception {
        Run run = runProbe(new String[] {"raise-term"}, null);

        String out = run.out();
        assertThat(run.exit()).as("128+15 on every host; output:%n%s", out).isEqualTo(143);
        assertThat(run.lines()).as("RELEASED is printed before the process ends").contains("RELEASED");
        assertThat(run.exitedAfterMs())
                .as("exit no earlier than the hold (%d ms minus the %d ms already elapsed)", HOLD_MS,
                        OrtGateHoldProbe.RAISE_AFTER_MS)
                .isGreaterThanOrEqualTo(HOLD_MS - OrtGateHoldProbe.RAISE_AFTER_MS - 300);
        assertThat(out).as("the deferral is logged").contains("ort_init_shutdown_wait in_flight=1");
        assertThat(out).doesNotContain("ort_init_signal_gate_unavailable signal=TERM")
                .doesNotContain("ort_init_signal_gate_unavailable signal=INT");
        if (!WindowsHost.isWindows(System.getProperty("os.name"))) {
            assertThat(out).as("off Windows every exit signal installs")
                    .doesNotContain("ort_init_signal_gate_unavailable");
        }
    }
}
