// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.function.Consumer;
import java.util.function.UnaryOperator;

/**
 * nexus-o5xyx.1 — placeholder for the shutdown-vs-model-init gate. INERT until
 * the fix lands: every method is a no-op, so the regression tests written
 * against this API fail for the real reason (a SIGTERM inside
 * {@code OrtSession} creation still crashes the JVM).
 */
public final class OrtInitGate {

    public static final long DEFAULT_WAIT_MILLIS = 8_000L;

    /** Thrown by {@link #enter(String)} once shutdown has begun. */
    public static final class ShutdownInProgressException extends IllegalStateException {
        public ShutdownInProgressException(String message) {
            super(message);
        }
    }

    /** Scope of one in-flight native model initialisation. */
    public interface Scope extends AutoCloseable {
        @Override
        void close();
    }

    private static final OrtInitGate PROCESS = new OrtInitGate();

    public static OrtInitGate process() {
        return PROCESS;
    }

    OrtInitGate() {}

    OrtInitGate(long waitMillis, Consumer<Thread> hookRegistrar) {}

    static long resolveWaitMillis(UnaryOperator<String> env) {
        return DEFAULT_WAIT_MILLIS;
    }

    public void installShutdownHook() {
        // inert
    }

    public Scope enter(String what) {
        return () -> { };
    }

    public boolean quiesce(long timeoutMillis) {
        return true;
    }
}
