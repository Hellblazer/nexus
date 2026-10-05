// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.IntConsumer;
import java.util.function.UnaryOperator;

/**
 * nexus-o5xyx.1 — defers SIGTERM-driven process exit until native ONNX Runtime
 * initialisation returns.
 *
 * <p><b>The defect.</b> A SIGTERM that arrives while an {@code OrtSession} is
 * being created (on {@code main} at boot, or on a request thread for the lazy
 * cross-encoder) made the JVM start its exit sequence. onnxruntime-java
 * registers its OWN shutdown hook ({@code OrtEnvironment$OrtEnvCloser}) that
 * releases the native {@code OrtEnv} and with it ORT's logging manager, and JVM
 * hooks run concurrently, so the environment was destroyed under the live
 * {@code InferenceSession::Initialize}. The session's {@code logging::Capture}
 * destructor then called {@code LoggingManager::Log} on freed state: SIGSEGV,
 * abort, exit 134, an {@code hs_err} file, a status a supervisor reads as a
 * crash. Native session creation cannot be cancelled.
 *
 * <p><b>Why not a shutdown hook of our own.</b> The first fix tried was a hook
 * that waits for in-flight inits. It does not work: hooks start together with
 * ORT's, in no order, so ORT's still destroys the environment while ours waits
 * (measured on hellmini: the crash stayed, with our wait logged as started).
 * Exit has to be held off BEFORE the hook phase begins.
 *
 * <p><b>The gate.</b> Every ORT init site wraps its native work in
 * {@link #enter(String)}. {@link #installSignalHandlers()} replaces the JVM's
 * default SIGTERM/SIGINT/SIGHUP handlers (which call {@code System.exit(128+n)}
 * immediately) with ones that, on a fresh thread, {@link #quiesce close the gate
 * to new inits and wait, bounded, for in-flight ones} and only then call
 * {@code System.exit(128+n)}, the exit status the default handlers produce.
 * On Windows the set is TERM, INT and BREAK (there is no HUP), BREAK being the CTRL_BREAK stop channel (see
 * {@link #exitSignals(String)}). BREAK has no exit-style default to replace:
 * HotSpot answers it with a thread dump and native-image ignores it, so there the
 * handler is what makes the process stop at all, and it must go through the gate
 * so a stop during ORT init is deferred like SIGTERM.
 * Once the gate is closed {@code enter} throws {@link ShutdownInProgressException},
 * so nothing starts native init while the process is exiting. Install it first
 * thing in {@code Main.main}.
 *
 * <p><b>Inference too</b> (nexus-o5xyx.3). {@code session.run()} logs through the
 * same manager ({@code ExecuteKernel}'s {@code Capture}), and a SIGTERM under
 * sustained embed load crashed the ungated process the same way (macOS arm64
 * laptop, ORT 1.20.0, 8 embed threads: SIGSEGV in {@code LoggingManager::Log} under
 * {@code InferenceSession::Run} on 7 of 80 signals ungated, 5 of 100 with this gate
 * covering init only). Waiting a run out is not enough: one bge sub-batch took
 * 2.7 s alone on that laptop and far longer with eight in flight, so a drain-only
 * gate hit its bound with 8 runs still live. Every run site ({@link Bge768Embedder},
 * {@link OnnxEmbedder}, {@link CrossEncoderReranker}) goes through {@link GatedRun}:
 * a scope from tensor creation to tensor release whose canceller sets ORT's run
 * terminate flag, so {@link #quiesce} stops in-flight runs at their next kernel
 * boundary, waits for them to return while the environment is still alive, and
 * refuses new ones. The names ({@code enter}, {@value #WAIT_ENV}, the
 * {@code ort_init_shutdown_wait*} events) predate that and are kept stable. A
 * refused or cancelled embed reaches the client as a retryable 503; a refused or
 * cancelled rerank degrades like any other local cross-encoder failure.
 *
 * <p><b>The bound</b> ({@value #WAIT_ENV}, default {@value #DEFAULT_WAIT_MILLIS} ms)
 * must sit UNDER every stop grace that ends in SIGKILL, because a wait that
 * outlives the grace is answered by SIGKILL, no better than the crash. The two
 * tightest are 5 s: the local supervisor's {@code _GRACEFUL_STOP_TIMEOUT}
 * ({@code src/nexus/daemon/storage_service_daemon.py}) and the test substrate's
 * teardown ({@code tests/_engine_substrate.py}); container stop graces are 10 s.
 * 3 s leaves 2 s of the tighter grace for the rest of shutdown. Measured bge-768
 * init on hellmini (M-series, external NVMe): 0.56 to 0.60 s warm (4 runs); after
 * {@code purge} 0.73, 0.73, 0.80, 0.94, 2.08, 4.83 and 4.97 s (7 runs). So 3 s
 * covers warm and most cold starts, and the worst cold start (about 5 s) does NOT
 * fit any bound that also fits a 5 s grace: there the wait expires and exit can
 * still crash. Raise the bound and the stop grace together on such hosts. On expiry
 * exit proceeds anyway and
 * logs {@code event=ort_init_shutdown_wait_timeout}; an operator whose disk makes
 * init slower than the bound can raise it together with the stop grace.
 * {@code tests/test_ort_init_gate_bound_lint.py} fails if the default reaches the
 * supervisor's grace.
 *
 * <p>Not covered: a {@code System.exit} called from application code while a
 * lazy init is in flight (the only callers are boot-time failure paths, which
 * run on the thread that would be initialising), and {@code Runtime.halt}
 * (the supervisor-death watchdog's deliberate hard kill).
 *
 * <p>Native image: the crash was NOT observed on the published mac-arm64 binary
 * (0 of 40 SIGTERM runs, 16 landing mid-init), but exposure is not excluded, and
 * Linux native binaries were not tested at all. The gate is installed there too
 * ({@code sun.misc.Signal} works under native-image; verified on mac-arm64).
 *
 * <p>Process-scoped by nature (the ORT environment and JVM signal dispositions
 * are process singletons), so production code shares {@link #process()}; tests
 * build private instances.
 */
public final class OrtInitGate {

    private static final Logger log = LoggerFactory.getLogger(OrtInitGate.class);

    /** Env var overriding the shutdown wait bound, in milliseconds. */
    public static final String WAIT_ENV = "NX_ORT_INIT_SHUTDOWN_WAIT_MS";

    /** Default shutdown wait bound; must stay under the supervisor's 5 s SIGKILL grace. */
    public static final long DEFAULT_WAIT_MILLIS = 3_000L;

    /** POSIX: signals whose default JVM handler is {@code System.exit(128 + n)}. */
    private static final List<String> POSIX_EXIT_SIGNALS = List.of("TERM", "INT", "HUP");

    /** Windows: TERM and INT install, HUP does not exist there, BREAK is the stop channel. */
    private static final List<String> WINDOWS_EXIT_SIGNALS = List.of("TERM", "INT", "BREAK");

    /**
     * The signal set to take over on the OS named {@code osName} ({@code os.name}). Each set is what
     * {@code Signal.handle} accepts there, so a normal boot never logs
     * {@code ort_init_signal_gate_unavailable}.
     *
     * <p><b>Windows: TERM, INT, BREAK.</b> The stop channel is CTRL_BREAK (RDR-224 Gap 4, nexus-f9bgu.8), and
     * a native-image process silently ignores it unless {@code BREAK} has a handler. BREAK is not an
     * exit-style signal: HotSpot's default for it is a thread dump, not {@code System.exit}, and HotSpot
     * refuses {@code Signal.handle} for it ({@code Signal already used by VM or OS: SIGBREAK}), so on a
     * Windows JVM, as opposed to the native engine, BREAK is logged unavailable and skipped. {@code HUP} is
     * left out. Measured 2026-10-05 on Windows 11 (GraalVM 25.0.3): {@code Signal.handle(new Signal("HUP"),
     * ..)} throws {@code IllegalArgumentException: Unknown signal: HUP} in both JVM and native-image mode,
     * while TERM and INT install in both and BREAK installs in native-image.
     *
     * <p><b>Elsewhere: TERM, INT, HUP.</b> {@code BREAK} is requested ONLY on Windows. Measured 2026-10-05:
     * on macOS arm64 (JDK 25.0.3), Linux amd64 and Linux arm64 (Temurin 25.0.4)
     * {@code Signal.handle(new Signal("BREAK"), ..)} throws {@code IllegalArgumentException: Unknown signal:
     * BREAK}.
     */
    static List<String> exitSignals(String osName) {
        boolean windows = osName != null && osName.toLowerCase(java.util.Locale.ROOT).startsWith("windows");
        return windows ? WINDOWS_EXIT_SIGNALS : POSIX_EXIT_SIGNALS;
    }

    /** Thrown by {@link #enter(String)} once shutdown has begun. */
    public static final class ShutdownInProgressException extends IllegalStateException {
        public ShutdownInProgressException(String message) {
            super(message);
        }
    }

    /**
     * Scope of one in-flight piece of native ORT work; closing it twice is harmless.
     * A scope may carry a canceller that {@link #quiesce} runs when shutdown begins
     * (nexus-o5xyx.3: an inference is cancelled rather than waited for).
     */
    public final class Scope implements AutoCloseable {
        private boolean left;
        private boolean cancelled;
        private Runnable canceller;

        private Scope() {}

        /**
         * Register (or, with {@code null}, clear) the action that stops this work
         * when shutdown begins. If shutdown has already begun it runs now. Clear it
         * before releasing anything the canceller touches.
         */
        public void onShutdown(Runnable c) {
            synchronized (lock) {
                if (left) return;
                canceller = c;
                if (c != null && closed && !cancelled) runCanceller(this);
            }
        }

        /** True once shutdown ran this scope's canceller and it returned normally. */
        public boolean cancelled() {
            synchronized (lock) {
                return cancelled;
            }
        }

        @Override
        public void close() {
            synchronized (lock) {
                if (left) return;
                left = true;
                canceller = null;
                active.remove(this);
                inFlight--;
                lock.notifyAll();
            }
        }
    }

    /** Test seam over {@code sun.misc.Signal}: route signal {@code name} to {@code onSignal(exitStatus)}. */
    interface SignalInstaller {
        void install(String name, IntConsumer onSignal);
    }

    private static final OrtInitGate PROCESS = new OrtInitGate();

    /** The process-wide gate. */
    public static OrtInitGate process() {
        return PROCESS;
    }

    private final Object lock = new Object();
    private int inFlight;
    private boolean closed;
    /** Open scopes, so {@link #quiesce} can run their cancellers. Guarded by {@link #lock}. */
    private final java.util.Set<Scope> active =
            java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());

    private final AtomicBoolean installed = new AtomicBoolean();
    private final long waitMillis;
    private final SignalInstaller signals;
    private final IntConsumer exitAction;
    private final List<String> exitSignals;

    OrtInitGate() {
        this(resolveWaitMillis(System::getenv), OrtInitGate::installJvmSignal, System::exit,
                System.getProperty("os.name"));
    }

    /** Test seam: explicit bound, signal installer and exit action, on this JVM's own OS. */
    OrtInitGate(long waitMillis, SignalInstaller signals, IntConsumer exitAction) {
        this(waitMillis, signals, exitAction, System.getProperty("os.name"));
    }

    /** Test seam: as above with the OS name injected, so the Windows signal set runs on any host. */
    OrtInitGate(long waitMillis, SignalInstaller signals, IntConsumer exitAction, String osName) {
        this.waitMillis = waitMillis;
        this.signals = signals;
        this.exitAction = exitAction;
        this.exitSignals = exitSignals(osName);
    }

    /** {@value #WAIT_ENV} as milliseconds, or {@link #DEFAULT_WAIT_MILLIS} when unset or invalid. */
    static long resolveWaitMillis(UnaryOperator<String> env) {
        String raw = env.apply(WAIT_ENV);
        if (raw == null || raw.isBlank()) return DEFAULT_WAIT_MILLIS;
        try {
            long v = Long.parseLong(raw.trim());
            if (v >= 0) return v;
        } catch (NumberFormatException ignored) {
            // fall through to the default below
        }
        log.warn("event=ort_init_shutdown_wait_invalid var={} value=\"{}\" using_ms={}",
                WAIT_ENV, raw, DEFAULT_WAIT_MILLIS);
        return DEFAULT_WAIT_MILLIS;
    }

    private static void installJvmSignal(String name, IntConsumer onSignal) {
        sun.misc.Signal.handle(new sun.misc.Signal(name), sig -> onSignal.accept(128 + sig.getNumber()));
    }

    /**
     * Take over the JVM's exit signals so process exit waits for in-flight
     * native model inits. Idempotent. Call before the first {@link #enter}.
     * A signal that cannot be installed on this platform (or a runtime without
     * {@code jdk.unsupported}) is logged and skipped; the process then behaves
     * exactly as it did before this gate existed.
     */
    public void installSignalHandlers() {
        if (!installed.compareAndSet(false, true)) return;
        for (String name : exitSignals) {
            try {
                signals.install(name, this::onExitSignal);
            } catch (IllegalArgumentException | LinkageError e) {
                log.warn("event=ort_init_signal_gate_unavailable signal={} error=\"{}\"", name, e.toString());
            }
        }
    }

    /** Runs on the JVM's signal thread: hand the wait to a fresh thread, never block dispatch. */
    private void onExitSignal(int exitStatus) {
        Thread t = new Thread(() -> {
            quiesce(waitMillis);
            exitAction.accept(exitStatus);
        }, "ort-init-deferred-exit");
        t.start();
    }

    /**
     * Mark one native model initialisation as in flight. Close the returned
     * scope in a {@code finally} once the init has returned or failed.
     *
     * @throws ShutdownInProgressException if shutdown has already begun; nothing
     *         was entered and the caller must not touch ORT
     */
    public Scope enter(String what) {
        synchronized (lock) {
            if (closed) {
                throw new ShutdownInProgressException(
                        "refusing to start native model init '" + what + "': shutdown in progress");
            }
            inFlight++;
            Scope s = new Scope();
            active.add(s);
            return s;
        }
    }

    /**
     * Caller holds {@link #lock}. The scope counts as cancelled only when its canceller
     * returned normally; one that throws is logged as {@code ort_run_cancel_failed},
     * the work is left to finish, and the wait still bounds exit.
     *
     * @return true when the canceller returned normally
     */
    private static boolean runCanceller(Scope s) {
        try {
            s.canceller.run();
            s.cancelled = true;
            return true;
        } catch (RuntimeException e) {
            log.warn("event=ort_run_cancel_failed error=\"{}\"", e.toString());
            return false;
        }
    }

    /**
     * Close the gate to new native work, run the canceller of every open scope
     * (an in-flight {@link GatedRun} is told to terminate), and wait up to
     * {@code timeoutMillis} for all in-flight work to return. Inits have no
     * canceller and are waited for.
     *
     * @return true when nothing is in flight on return; false on timeout or interrupt
     */
    public boolean quiesce(long timeoutMillis) {
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMillis);
        synchronized (lock) {
            closed = true;
            int cancelled = 0;
            // A snapshot: a canceller may close its own scope on this thread (the lock is reentrant).
            for (Scope s : List.copyOf(active)) {
                if (s.canceller != null && !s.cancelled && runCanceller(s)) {
                    cancelled++;
                }
            }
            if (cancelled > 0) {
                log.info("event=ort_run_cancelled count={}", cancelled);
            }
            if (inFlight > 0) {
                log.info("event=ort_init_shutdown_wait in_flight={} bound_ms={}", inFlight, timeoutMillis);
            }
            long t0 = System.nanoTime();
            while (inFlight > 0) {
                long remainingMs = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime());
                if (remainingMs <= 0) {
                    log.warn("event=ort_init_shutdown_wait_timeout in_flight={} bound_ms={}",
                            inFlight, timeoutMillis);
                    return false;
                }
                try {
                    lock.wait(remainingMs);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return false;
                }
            }
            long waitedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);
            if (waitedMs > 0) {
                log.info("event=ort_init_shutdown_wait_done waited_ms={}", waitedMs);
            }
            return true;
        }
    }
}
