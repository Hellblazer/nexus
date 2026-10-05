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

    // ── cancellers (nexus-o5xyx.3: in-flight inference is cancelled, not waited out) ──

    @Test
    void quiesceRunsTheCancellerOfEveryOpenScopeThenWaits() throws Exception {
        OrtInitGate gate = new OrtInitGate();
        OrtInitGate.Scope scope = gate.enter("run");
        java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
        // A cancelled run returns promptly: model that by closing the scope from the canceller's thread.
        scope.onShutdown(() -> {
            calls.incrementAndGet();
            new Thread(scope::close).start();
        });

        long t0 = System.nanoTime();
        assertThat(gate.quiesce(10_000)).as("the cancelled run returned, so quiesce is clean").isTrue();
        assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0))
                .as("cancellation, not the bound, ended the wait").isLessThan(5_000L);
        assertThat(calls).as("the canceller runs exactly once").hasValue(1);
        assertThat(scope.cancelled()).isTrue();
    }

    @Test
    void aCancellerRegisteredAfterShutdownBeganRunsAtOnce() {
        OrtInitGate gate = new OrtInitGate();
        OrtInitGate.Scope scope = gate.enter("run");
        gate.quiesce(0);
        AtomicBoolean ran = new AtomicBoolean();
        scope.onShutdown(() -> ran.set(true));
        assertThat(ran).as("the race between enter() and registration must not lose the cancel").isTrue();
        assertThat(scope.cancelled()).isTrue();
    }

    @Test
    void aClearedOrClosedScopesCancellerNeverRuns() {
        OrtInitGate gate = new OrtInitGate();
        AtomicBoolean ran = new AtomicBoolean();
        OrtInitGate.Scope cleared = gate.enter("cleared");
        cleared.onShutdown(() -> ran.set(true));
        cleared.onShutdown(null);
        OrtInitGate.Scope closed = gate.enter("closed");
        closed.onShutdown(() -> ran.set(true));
        closed.close();

        gate.quiesce(0);
        assertThat(ran).as("a canceller may touch released native state once cleared or closed").isFalse();
        assertThat(cleared.cancelled()).isFalse();
    }

    @Test
    void aCancellerThatClosesItsOwnScopeSynchronouslyIsSafe() {
        OrtInitGate gate = new OrtInitGate();
        OrtInitGate.Scope a = gate.enter("a");
        OrtInitGate.Scope b = gate.enter("b");
        a.onShutdown(a::close);
        b.onShutdown(b::close);
        assertThat(gate.quiesce(1_000)).as("both closed from inside the cancel pass").isTrue();
    }

    @Test
    void aThrowingCancellerIsLoggedAndTheWaitStillBoundsExit() {
        OrtInitGate gate = new OrtInitGate();
        OrtInitGate.Scope scope = gate.enter("run");
        scope.onShutdown(() -> { throw new IllegalStateException("boom"); });
        assertThat(gate.quiesce(100)).as("the run never returned; the bound ends the wait").isFalse();
        assertThat(scope.cancelled()).as("a canceller that threw did not cancel anything").isFalse();
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
        OrtInitGate gate = new OrtInitGate(5_000, signals, status -> { }, "Linux");
        gate.installSignalHandlers();
        var first = new java.util.LinkedHashMap<>(signals.handlers);
        gate.installSignalHandlers();
        assertThat(signals.handlers.keySet()).containsExactly("TERM", "INT", "HUP");
        assertThat(signals.handlers).as("second install is a no-op").isEqualTo(first);
    }

    // ── CTRL_BREAK on Windows (nexus-f9bgu.8, RDR-224 Gap 4) ───────────────────

    @Test
    void onWindowsTheInstalledSetAlsoIncludesBreak() {
        FakeSignals signals = new FakeSignals();
        new OrtInitGate(5_000, signals, status -> { }, "Windows 11").installSignalHandlers();
        assertThat(signals.handlers.keySet())
                .as("CTRL_BREAK reaches a native-image process only through a BREAK handler; "
                        + "without one it is silently ignored")
                .containsExactly("TERM", "INT", "HUP", "BREAK");
    }

    @Test
    void windowsIsDetectedFromTheOsNameCaseInsensitively() {
        assertThat(OrtInitGate.exitSignals("Windows Server 2022")).contains("BREAK");
        assertThat(OrtInitGate.exitSignals("WINDOWS 10")).contains("BREAK");
        assertThat(OrtInitGate.exitSignals("Mac OS X")).doesNotContain("BREAK");
        assertThat(OrtInitGate.exitSignals("Linux")).doesNotContain("BREAK");
        assertThat(OrtInitGate.exitSignals(null)).as("an unreadable os.name is not Windows")
                .doesNotContain("BREAK");
    }

    /**
     * Measured 2026-10-05 on macOS arm64 (GraalVM JDK 25.0.3), Linux amd64 and Linux arm64 (Temurin
     * 25.0.4): {@code Signal.handle(new Signal("BREAK"), ..)} throws
     * {@code IllegalArgumentException: Unknown signal: BREAK}. This installer reproduces that, so a
     * BREAK request on a POSIX gate would log {@code ort_init_signal_gate_unavailable} on every boot.
     */
    private static final class PosixLikeSignals implements OrtInitGate.SignalInstaller {
        final List<String> requested = new ArrayList<>();
        @Override
        public void install(String name, java.util.function.IntConsumer onSignal) {
            requested.add(name);
            if (name.equals("BREAK")) {
                throw new IllegalArgumentException("Unknown signal: BREAK");
            }
        }
    }

    @Test
    void onPosixBreakIsNeverRequestedSoNoUnavailableWarningIsLogged() {
        for (String os : List.of("Linux", "Mac OS X")) {
            PosixLikeSignals signals = new PosixLikeSignals();
            List<String> logs = captureLogs(() ->
                    new OrtInitGate(5_000, signals, status -> { }, os).installSignalHandlers());
            assertThat(signals.requested).as(os + ": BREAK is not asked of a POSIX JVM")
                    .containsExactly("TERM", "INT", "HUP");
            assertThat(logs).as(os + ": no unavailable warning for any signal")
                    .noneMatch(l -> l.contains("ort_init_signal_gate_unavailable"));
        }
    }

    @Test
    void theWindowsGateOnAPosixLikeJvmDoesLogTheUnavailableWarningForBreak() {
        // Non-vacuity of the test above: the capture sees the warning when BREAK is requested.
        PosixLikeSignals signals = new PosixLikeSignals();
        List<String> logs = captureLogs(() ->
                new OrtInitGate(5_000, signals, status -> { }, "Windows 11").installSignalHandlers());
        assertThat(signals.requested).contains("BREAK");
        assertThat(logs).anyMatch(l -> l.contains("ort_init_signal_gate_unavailable") && l.contains("BREAK"));
    }

    @Test
    void aBreakSignalRunsTheDeferredExitWithItsStatus() throws Exception {
        FakeSignals signals = new FakeSignals();
        java.util.concurrent.atomic.AtomicInteger exited = new java.util.concurrent.atomic.AtomicInteger(-1);
        CountDownLatch exitCalled = new CountDownLatch(1);
        OrtInitGate gate = new OrtInitGate(10_000, signals, status -> {
            exited.set(status);
            exitCalled.countDown();
        }, "Windows 11");
        gate.installSignalHandlers();

        OrtInitGate.Scope scope = gate.enter("test-model");
        signals.handlers.get("BREAK").accept(149); // 128 + 21, the status the spike measured
        assertThat(exitCalled.await(400, TimeUnit.MILLISECONDS))
                .as("a stop during ORT init is deferred exactly as for SIGTERM").isFalse();
        scope.close();
        assertThat(exitCalled.await(5, TimeUnit.SECONDS)).isTrue();
        assertThat(exited.get()).isEqualTo(149);
    }

    private static List<String> captureLogs(Runnable body) {
        ch.qos.logback.classic.Logger target =
                (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OrtInitGate.class);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
                new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        target.addAppender(logs);
        try {
            body.run();
            return logs.list.stream().map(e -> e.getLevel() + " " + e.getFormattedMessage()).toList();
        } finally {
            target.detachAppender(logs);
            logs.stop();
        }
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

    // ── the run lint (nexus-o5xyx.3): every session.run( passes a GatedRun's options ──

    /** Every variable or field declared as an {@code OrtSession} (e.g. {@code session}, {@code sess}). */
    private static final java.util.regex.Pattern ORT_SESSION_DECL = java.util.regex.Pattern.compile(
            "\\bOrtSession\\s+(\\w+)\\s*[;=,)]");
    private static final java.util.regex.Pattern GATED_RUN_OPEN = java.util.regex.Pattern.compile(
            "try\\s*\\(\\s*GatedRun\\s+(\\w+)\\s*=\\s*GatedRun\\s*\\.\\s*open\\(");

    /** {@code <name>.run(} for every OrtSession name declared in {@code code}, plus {@code session}. */
    private static java.util.regex.Pattern sessionRun(String code) {
        var names = new java.util.TreeSet<String>(List.of("session"));
        ORT_SESSION_DECL.matcher(code).results().forEach(m -> names.add(m.group(1)));
        return java.util.regex.Pattern.compile(
                "\\b(?:" + String.join("|", names) + ")\\s*\\.\\s*run\\s*\\(");
    }

    /**
     * Problems for every {@code <session>.run(} (any OrtSession-typed name in the file)
     * whose argument list does not pass {@code X.options()}, where {@code X} is the
     * resource of the nearest preceding {@code try (GatedRun X = GatedRun.open(...))}.
     */
    static List<String> ungatedRunCalls(String name, String source) {
        String code = stripComments(source);
        List<String> problems = new ArrayList<>();
        var calls = sessionRun(code).matcher(code);
        while (calls.find()) {
            int depth = 1;
            int i = calls.end();
            while (i < code.length() && depth > 0) {
                char c = code.charAt(i++);
                if (c == '(') depth++;
                else if (c == ')') depth--;
            }
            String args = code.substring(calls.end(), i);
            String runVar = null;
            var opens = GATED_RUN_OPEN.matcher(code);
            while (opens.find() && opens.start() < calls.start()) runVar = opens.group(1);
            if (runVar == null || !args.matches("(?s).*\\b" + runVar + "\\s*\\.\\s*options\\s*\\(\\s*\\).*")) {
                problems.add(name + ": session.run( at offset " + calls.start()
                        + " does not pass a GatedRun's options() (a SIGTERM mid-run crashes the JVM)");
            }
        }
        return problems;
    }

    @Test
    void everySessionRunInMainSourcePassesAGatedRunsOptions() throws IOException {
        List<String> problems = new ArrayList<>();
        int calls = 0;
        try (var files = Files.walk(MAIN_SRC)) {
            for (Path f : (Iterable<Path>) files.filter(x -> x.toString().endsWith(".java"))::iterator) {
                String src = Files.readString(f);
                String code = stripComments(src);
                calls += (int) sessionRun(code).matcher(code).results().count();
                problems.addAll(ungatedRunCalls(f.getFileName().toString(), src));
            }
        }
        assertThat(problems).isEmpty();
        assertThat(calls).as("non-vacuity: session.run sites scanned (Bge768, OnnxEmbedder, CrossEncoder)")
                .isGreaterThanOrEqualTo(3);
    }

    @Test
    void theRunLintFlagsABareRun() {
        String gated = "try (GatedRun run = GatedRun.open(\"r\")) {\n"
                + "  try (var r = session.run(Map.of(\"a\", t), run.options())) { }\n}";
        assertThat(ungatedRunCalls("ok", gated)).isEmpty();
        assertThat(ungatedRunCalls("bare", "try (var r = session.run(inputs)) { }")).hasSize(1);
        assertThat(ungatedRunCalls("no-options",
                "try (GatedRun run = GatedRun.open(\"r\")) { session.run(inputs); }")).hasSize(1);
        assertThat(ungatedRunCalls("other-options",
                "try (GatedRun run = GatedRun.open(\"r\")) { session.run(inputs, opts); }")).hasSize(1);
        assertThat(ungatedRunCalls("commented", "// session.run(inputs);")).isEmpty();
        assertThat(ungatedRunCalls("other-name", "OrtSession sess = null; try (var r = sess.run(inputs)) { }"))
                .as("any OrtSession-typed name is checked, not only `session`").hasSize(1);
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
