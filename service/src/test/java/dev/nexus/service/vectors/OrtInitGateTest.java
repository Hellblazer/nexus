// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-o5xyx.1 — the gate that keeps process exit from overlapping native
 * model initialisation. The crash itself is proven end to end by
 * {@link OrtShutdownSafetyTest}; this class pins the gate's own contract and,
 * by source scan, that every ORT session site and {@code Main} are wired to it.
 */
@Timeout(value = 30, unit = TimeUnit.SECONDS)
class OrtInitGateTest {

    private static final Path MAIN_SRC = Path.of("src", "main", "java", "dev", "nexus", "service");

    @Test
    void quiesceWaitsForAnInFlightInitToFinish() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        OrtInitGate.Scope scope = gate.enter("test-model");

        AtomicBoolean quiesced = new AtomicBoolean();
        CountDownLatch returned = new CountDownLatch(1);
        Thread t = new Thread(() -> {
            quiesced.set(gate.quiesce(10_000));
            returned.countDown();
        });
        t.start();

        assertThat(returned.await(300, TimeUnit.MILLISECONDS))
                .as("quiesce must NOT return while an init is still in flight")
                .isFalse();
        scope.close();
        assertThat(returned.await(5, TimeUnit.SECONDS)).as("quiesce returns once init ends").isTrue();
        assertThat(quiesced).as("clean quiesce reports true").isTrue();
    }

    @Test
    void quiesceWithNothingInFlightReturnsImmediately() {
        assertThat(new OrtInitGate().quiesce(10_000)).isTrue();
    }

    @Test
    void quiesceIsBoundedWhenAnInitNeverFinishes() {
        OrtInitGate gate = new OrtInitGate();
        gate.enter("stuck-model");
        long t0 = System.nanoTime();
        boolean clean = gate.quiesce(200);
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);
        assertThat(clean).as("a timed-out quiesce reports false").isFalse();
        assertThat(elapsedMs).as("the wait honours its bound").isBetween(150L, 5_000L);
    }

    @Test
    void enterAfterShutdownBeganIsRefusedEvenIfQuiesceTimedOut() {
        OrtInitGate gate = new OrtInitGate();
        gate.enter("stuck-model");
        gate.quiesce(50);
        assertThatThrownBy(() -> gate.enter("late-model"))
                .isInstanceOf(OrtInitGate.ShutdownInProgressException.class)
                .hasMessageContaining("late-model");
    }

    @Test
    void closingAScopeTwiceCountsOnce() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        OrtInitGate.Scope a = gate.enter("a");
        OrtInitGate.Scope b = gate.enter("b");
        a.close();
        a.close();
        assertThat(gate.quiesce(100)).as("b is still in flight").isFalse();
        b.close();
        assertThat(gate.quiesce(100)).as("both closed").isTrue();
    }

    @Test
    void installedHookQuiescesAndInstallIsIdempotent() throws Exception {
        List<Thread> registered = new ArrayList<>();
        OrtInitGate gate = new OrtInitGate(5_000, registered::add);
        gate.installShutdownHook();
        gate.installShutdownHook();
        assertThat(registered).as("the hook registers exactly once").hasSize(1);

        OrtInitGate.Scope scope = gate.enter("test-model");
        Thread hook = registered.get(0);
        hook.start();
        hook.join(300);
        assertThat(hook.isAlive()).as("the hook blocks while an init is in flight").isTrue();
        scope.close();
        hook.join(5_000);
        assertThat(hook.isAlive()).as("the hook finishes once the init ends").isFalse();
        assertThatThrownBy(() -> gate.enter("after"))
                .isInstanceOf(OrtInitGate.ShutdownInProgressException.class);
    }

    @Test
    void waitBoundResolvesFromTheEnvironmentWithASafeFallback() {
        assertThat(OrtInitGate.resolveWaitMillis(k -> null)).isEqualTo(OrtInitGate.DEFAULT_WAIT_MILLIS);
        assertThat(OrtInitGate.resolveWaitMillis(k -> "2500")).isEqualTo(2500L);
        assertThat(OrtInitGate.resolveWaitMillis(k -> " 0 ")).isEqualTo(0L);
        assertThat(OrtInitGate.resolveWaitMillis(k -> "soon")).isEqualTo(OrtInitGate.DEFAULT_WAIT_MILLIS);
        assertThat(OrtInitGate.resolveWaitMillis(k -> "-5")).isEqualTo(OrtInitGate.DEFAULT_WAIT_MILLIS);
    }

    // ── wiring, by source scan (same pattern as OnnxIntraOpWiringTest) ─────────

    @Test
    void mainInstallsTheHookBeforeAnyModelIsConstructed() throws IOException {
        String main = Files.readString(MAIN_SRC.resolve("Main.java"));
        int hook = main.indexOf("OrtInitGate.process().installShutdownHook()");
        int bge = main.indexOf("new Bge768Embedder()");
        assertThat(hook).as("Main must install the OrtInitGate shutdown hook").isNotEqualTo(-1);
        assertThat(bge).as("Main constructs Bge768Embedder").isNotEqualTo(-1);
        assertThat(hook)
                .as("the hook must be installed BEFORE the first native model init — a SIGTERM "
                        + "before it exists is the nexus-o5xyx.1 crash")
                .isLessThan(bge);
    }

    @Test
    void everyOrtSessionSiteEntersTheGateBeforeCreatingTheSession() throws IOException {
        for (String file : List.of("Bge768Embedder.java", "OnnxEmbedder.java", "CrossEncoderReranker.java")) {
            String src = Files.readString(MAIN_SRC.resolve("vectors").resolve(file));
            int enter = src.indexOf("OrtInitGate.process().enter(");
            int env = src.indexOf("OrtEnvironment.getEnvironment()");
            int create = src.indexOf("createSession(");
            assertThat(enter).as("%s must enter the OrtInitGate", file).isNotEqualTo(-1);
            assertThat(enter)
                    .as("%s must enter the gate before OrtEnvironment.getEnvironment() "
                            + "(the ORT logging manager is created there)", file)
                    .isLessThan(env);
            assertThat(enter).as("%s must enter the gate before createSession", file).isLessThan(create);
        }
    }
}
