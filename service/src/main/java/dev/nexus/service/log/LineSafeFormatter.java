// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import dev.nexus.service.http.LogText;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.logging.ConsoleHandler;
import java.util.logging.Handler;
import java.util.logging.LogRecord;
import java.util.logging.SimpleFormatter;

/**
 * The JDK's {@link SimpleFormatter} with every control character of a record's message and of its exception messages written
 * as an escape ({@link LogText#lineSafe}), so one record is the same physical lines it always was and a value in it cannot
 * add one (nexus-dngtz.1.11, RDR-167).
 *
 * <p>Liquibase logs through java.util.logging, not logback: its records reach the JVM's {@link ConsoleHandler} with the
 * JDK's formatter, a {@code <date> <source>} line and then {@code LEVEL: message}. The supervisor reads that second line by
 * its {@code LEVEL: } prefix ({@code INFO: Waiting for changelog lock}), so the shape stays. Liquibase puts the PostgreSQL
 * error text of a failed changeset in a message, and PostgreSQL echoes stored values in it.
 *
 * <p>A record with nothing to escape formats byte for byte as {@link SimpleFormatter} formats it, the JDK's
 * {@code java.util.logging.SimpleFormatter.format} property included: the work is done by the JDK formatter on a copy of the
 * record whose message is escaped and whose exception is a copy with escaped messages, so a record with an exception
 * matches it too, the blank line it ends with included.
 */
public class LineSafeFormatter extends SimpleFormatter {

    private static final Logger log = LoggerFactory.getLogger(LineSafeFormatter.class);

    /** How deep causes and suppressed exceptions are followed: a cycle through two causes would not end otherwise. */
    private static final int MAX_DEPTH = 32;

    @Override
    public String format(LogRecord record) {
        var copy = new LogRecord(record.getLevel(), LogText.lineSafe(formatMessage(record)));
        copy.setInstant(record.getInstant());
        copy.setLoggerName(record.getLoggerName());
        copy.setSourceClassName(record.getSourceClassName());
        copy.setSourceMethodName(record.getSourceMethodName());
        copy.setLongThreadID(record.getLongThreadID());
        copy.setSequenceNumber(record.getSequenceNumber());
        Throwable thrown = record.getThrown();
        if (thrown != null) {
            // the JDK formatter prints the trace itself, in whatever place its format property puts it, so a record with an
            // exception is byte for byte the JDK's too (its closing blank line included)
            copy.setThrown(new SafeTrace(thrown));
        }
        return super.format(copy);
    }

    /** A throwable that prints as {@code original} does with its message escaped; its causes and suppressed ones likewise. */
    private static final class SafeTrace extends Throwable {
        private final String text;

        SafeTrace(Throwable original) {
            this(original, 0);
        }

        private SafeTrace(Throwable original, int depth) {
            // the cause goes in through the constructor: initCause is refused on a throwable made with a null cause
            super(null, causeOf(original, depth), true, true);   // suppression and a writable trace, so the copy holds both
            this.text = describe(original);
            setStackTrace(original.getStackTrace());
            if (depth < MAX_DEPTH) {
                for (Throwable s : original.getSuppressed()) {
                    addSuppressed(new SafeTrace(s, depth + 1));
                }
            }
        }

        /** {@code <class>: <message>} as Throwable.toString makes it; the class name alone if the message cannot be read. */
        private static String describe(Throwable original) {
            String name = original.getClass().getName();
            try {
                String message = original.getLocalizedMessage();
                return message == null ? name : name + ": " + message;
            } catch (RuntimeException | LinkageError | StackOverflowError e) {
                return name;   // a throwable whose getMessage throws must not cost the record
            }
        }

        private static Throwable causeOf(Throwable original, int depth) {
            Throwable cause = original.getCause();
            return depth < MAX_DEPTH && cause != null && cause != original ? new SafeTrace(cause, depth + 1) : null;
        }

        @Override
        public String toString() {
            return LogText.lineSafe(text);
        }
    }

    /**
     * Puts this formatter on every console handler of the root logger, the handler the JVM's default configuration gives
     * Liquibase's records. A handler that is not a console handler is left alone, as is one that has it already.
     *
     * @return how many console handlers now use it
     */
    public static int install() {
        int n = 0;
        try {
            for (Handler handler : java.util.logging.Logger.getLogger("").getHandlers()) {
                if (handler instanceof ConsoleHandler) {
                    if (!(handler.getFormatter() instanceof LineSafeFormatter)) {
                        handler.setFormatter(new LineSafeFormatter());
                    }
                    n++;
                }
            }
        } catch (RuntimeException | LinkageError e) {
            log.warn("event=jul_line_safe_failed cause=\"{}\"", LogText.lineSafe(SecretMask.mask(e.toString())));
        }
        return n;
    }
}
