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
 * by source scan, that every ORT init call in service/src/main is gated and
 * {@code Main} installs the gate first.
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

    /** Records the handlers a gate installs, instead of touching real JVM signals. */
    private static final class FakeSignals implements OrtInitGate.SignalInstaller {
        final java.util.Map<String, java.util.function.IntConsumer> handlers = new java.util.LinkedHashMap<>();
        @Override
        public void install(String name, java.util.function.IntConsumer onSignal) {
            handlers.put(name, onSignal);
        }
    }

    @Test
    void installTakesOverTermIntHupExactlyOnce() {
        FakeSignals signals = new FakeSignals();
        OrtInitGate gate = new OrtInitGate(5_000, signals, status -> { });
        gate.installSignalHandlers();
        var first = new java.util.LinkedHashMap<>(signals.handlers);
        gate.installSignalHandlers();
        assertThat(signals.handlers.keySet()).containsExactly("TERM", "INT", "HUP");
        assertThat(signals.handlers).as("second install is a no-op").isEqualTo(first);
    }

    @Test
    void aSignalDuringAnInitDefersExitUntilTheInitEndsThenExitsWithTheSignalStatus() throws Exception {
        FakeSignals signals = new FakeSignals();
        java.util.concurrent.atomic.AtomicInteger exited = new java.util.concurrent.atomic.AtomicInteger(-1);
        CountDownLatch exitCalled = new CountDownLatch(1);
        OrtInitGate gate = new OrtInitGate(10_000, signals, status -> {
            exited.set(status);
            exitCalled.countDown();
        });
        gate.installSignalHandlers();

        OrtInitGate.Scope scope = gate.enter("test-model");
        signals.handlers.get("TERM").accept(143);

        assertThat(exitCalled.await(400, TimeUnit.MILLISECONDS))
                .as("exit must be DEFERRED while an init is in flight")
                .isFalse();
        assertThatThrownBy(() -> gate.enter("late-model"))
                .as("no new native init may start once shutdown began")
                .isInstanceOf(OrtInitGate.ShutdownInProgressException.class);

        scope.close();
        assertThat(exitCalled.await(5, TimeUnit.SECONDS)).as("exit proceeds once the init ends").isTrue();
        assertThat(exited.get()).as("exit status is the default handler's 128+n").isEqualTo(143);
    }

    @Test
    void aSignalWithNothingInFlightExitsPromptly() throws Exception {
        FakeSignals signals = new FakeSignals();
        java.util.concurrent.atomic.AtomicInteger exited = new java.util.concurrent.atomic.AtomicInteger(-1);
        CountDownLatch exitCalled = new CountDownLatch(1);
        OrtInitGate gate = new OrtInitGate(10_000, signals, status -> {
            exited.set(status);
            exitCalled.countDown();
        });
        gate.installSignalHandlers();
        signals.handlers.get("INT").accept(130);
        assertThat(exitCalled.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(exited.get()).isEqualTo(130);
    }

    @Test
    void aStuckInitDoesNotHoldExitPastTheBound() throws Exception {
        FakeSignals signals = new FakeSignals();
        CountDownLatch exitCalled = new CountDownLatch(1);
        OrtInitGate gate = new OrtInitGate(300, signals, status -> exitCalled.countDown());
        gate.installSignalHandlers();
        gate.enter("stuck-model"); // never closed
        signals.handlers.get("TERM").accept(143);
        assertThat(exitCalled.await(5, TimeUnit.SECONDS))
                .as("exit proceeds after the bound even if the init never returns")
                .isTrue();
    }

    @Test
    void anUnavailableSignalIsSkippedNotFatal() {
        OrtInitGate gate = new OrtInitGate(5_000, (name, h) -> {
            throw new IllegalArgumentException("Unknown signal: " + name);
        }, status -> { });
        gate.installSignalHandlers(); // must not throw
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
    void mainInstallsTheGateBeforeAnyModelIsConstructed() throws IOException {
        String main = Files.readString(MAIN_SRC.resolve("Main.java"));
        int hook = main.indexOf("OrtInitGate.process().installSignalHandlers()");
        int bge = main.indexOf("new Bge768Embedder()");
        assertThat(hook).as("Main must install the OrtInitGate signal handlers").isNotEqualTo(-1);
        assertThat(bge).as("Main constructs Bge768Embedder").isNotEqualTo(-1);
        assertThat(hook)
                .as("the gate must be installed BEFORE the first native model init — a SIGTERM "
                        + "before it exists is the nexus-o5xyx.1 crash")
                .isLessThan(bge);
    }

    // ── the lint: no ORT init call outside a gate scope ───────────────────────

    private static final java.util.regex.Pattern ORT_CALL = java.util.regex.Pattern.compile(
            "OrtEnvironment\\s*\\.\\s*getEnvironment\\s*\\(|\\.\\s*createSession\\s*\\(");
    private static final java.util.regex.Pattern GATE_ENTER = java.util.regex.Pattern.compile(
            "(\\w+)\\s*=\\s*OrtInitGate\\s*\\.\\s*process\\(\\)\\s*\\.\\s*enter\\(");

    private static String stripComments(String source) {
        return source.replaceAll("(?s)/\\*.*?\\*/", "").replaceAll("//[^\\n]*", "");
    }

    /**
     * Problems for every {@code OrtEnvironment.getEnvironment()} / {@code createSession(}
     * call in {@code source} that is not preceded by {@code X = OrtInitGate.process().enter(...)}
     * with a {@code try} between the two, and followed by {@code finally { ... X.close() }}. Comments are ignored.
     */
    static List<String> ungatedOrtCalls(String name, String source) {
        String code = stripComments(source);
        List<String> problems = new ArrayList<>();
        var calls = ORT_CALL.matcher(code);
        while (calls.find()) {
            int at = calls.start();
            String scopeVar = null;
            int enterEnd = -1;
            var enters = GATE_ENTER.matcher(code);
            while (enters.find() && enters.start() < at) { scopeVar = enters.group(1); enterEnd = enters.end(); }
            String call = calls.group().replaceAll("\\s+", "");
            if (scopeVar == null) {
                problems.add(name + ": " + call + " at offset " + at + " has no preceding OrtInitGate enter()");
            } else if (!java.util.regex.Pattern.compile("\\btry\\b").matcher(code.substring(enterEnd, at)).find()) {
                problems.add(name + ": " + call + " at offset " + at + " runs between enter() and the try, "
                        + "so an exception there leaks the scope");
            } else if (!java.util.regex.Pattern
                    .compile("finally\\s*\\{[^}]*\\b" + scopeVar + "\\.close\\(\\)")
                    .matcher(code.substring(at)).find()) {
                problems.add(name + ": " + call + " at offset " + at + " is not followed by a finally that closes "
                        + scopeVar);
            }
        }
        return problems;
    }

    @Test
    void everyOrtInitCallInMainSourceSitsInsideAGateScope() throws IOException {
        List<String> problems = new ArrayList<>();
        int filesWithCalls = 0;
        int calls = 0;
        try (var files = Files.walk(MAIN_SRC)) {
            for (Path f : (Iterable<Path>) files.filter(x -> x.toString().endsWith(".java"))::iterator) {
                String src = Files.readString(f);
                int n = (int) ORT_CALL.matcher(stripComments(src)).results().count();
                if (n > 0) { filesWithCalls++; calls += n; }
                problems.addAll(ungatedOrtCalls(f.getFileName().toString(), src));
            }
        }
        assertThat(problems)
                .as("every OrtEnvironment.getEnvironment()/createSession( call in service/src/main must sit "
                        + "between OrtInitGate.process().enter(..) and a finally that closes the scope; an "
                        + "ungated ORT site reopens nexus-o5xyx.1 (SIGTERM during init crashes the JVM)")
                .isEmpty();
        // Non-vacuity: a scan that found nothing is a failure, not a pass.
        assertThat(filesWithCalls).as("files containing ORT init calls (Bge768, OnnxEmbedder, CrossEncoder)")
                .isGreaterThanOrEqualTo(3);
        assertThat(calls).as("ORT init call sites scanned").isGreaterThanOrEqualTo(6);
    }

    @Test
    void theLintFlagsAnUngatedOrGatedTooLooselySite() {
        String gated = "OrtInitGate.Scope s = OrtInitGate.process().enter(\"m\");\n"
                + "try { env = OrtEnvironment.getEnvironment(); x = env.createSession(p, o); }\n"
                + "finally { s.close(); }";
        assertThat(ungatedOrtCalls("ok", gated)).isEmpty();

        assertThat(ungatedOrtCalls("bare", "x = env.createSession(p, o);")).hasSize(1);
        assertThat(ungatedOrtCalls("bare-env", "e = OrtEnvironment.getEnvironment();")).hasSize(1);
        assertThat(ungatedOrtCalls("enter-after",
                "x = env.createSession(p, o); OrtInitGate.Scope s = OrtInitGate.process().enter(\"m\");"
                        + " finally { s.close(); }")).hasSize(1);
        assertThat(ungatedOrtCalls("no-finally",
                "OrtInitGate.Scope s = OrtInitGate.process().enter(\"m\"); x = env.createSession(p, o); s.close();"))
                .hasSize(1);
        assertThat(ungatedOrtCalls("getenv-outside",
                "OrtInitGate.Scope s = OrtInitGate.process().enter(\"m\");\n"
                        + "e = OrtEnvironment.getEnvironment();\n"
                        + "try { x = e.createSession(p, o); } finally { s.close(); }"))
                .as("getEnvironment between enter and the try leaks the scope on an exception")
                .hasSize(1);
        assertThat(ungatedOrtCalls("commented",
                "// x = env.createSession(p, o);\n/* OrtEnvironment.getEnvironment() */")).isEmpty();
    }
}
