// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import dev.nexus.service.http.LogText;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Sends an exception no thread caught to the engine's log, through the logback pattern (nexus-dngtz.1.11, RDR-167). The JVM's
 * default handler prints {@code Exception in thread "x" <class>: <message>} straight to stderr, past logback and its escape,
 * and a line break in the message would start a raw line there. The engine installs no handler of its own and writes
 * nothing to stderr, so this is the only path to it.
 *
 * <p>The message names the exception class only; the throwable is the last argument, so the exception converter writes its
 * message, its causes' and its suppressed ones' escaped. The handler never throws: a failure to log is dropped, not raised
 * in the thread that is already dying.
 */
public final class UncaughtExceptionLog {

    private static final Logger log = LoggerFactory.getLogger(UncaughtExceptionLog.class);

    private UncaughtExceptionLog() { /* static */ }

    /** Makes this the default handler of every thread that sets none. Never throws. */
    public static void install() {
        try {
            Thread.setDefaultUncaughtExceptionHandler(UncaughtExceptionLog::report);
        } catch (RuntimeException | LinkageError ignored) {
            // the JVM keeps its default handler; the engine boots without this one
        }
    }

    static void report(Thread thread, Throwable thrown) {
        try {
            log.error("event=uncaught_exception thread={} exception={}", LogText.mdcValue(thread.getName()),
                thrown.getClass().getName(), thrown);
        } catch (Throwable ignored) {
            // nothing more can be done in a dying thread, and a handler that throws is ignored by the JVM anyway
        }
    }
}
