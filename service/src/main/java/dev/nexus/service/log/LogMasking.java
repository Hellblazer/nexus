// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.Appender;
import ch.qos.logback.core.ConsoleAppender;
import ch.qos.logback.core.OutputStreamAppender;
import ch.qos.logback.core.spi.AppenderAttachable;
import org.slf4j.ILoggerFactory;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.function.Supplier;

/**
 * Puts {@link MaskingPatternLayoutEncoder} on every pattern-layout appender of the logging context, so {@link SecretMask}
 * covers every line the engine writes and every stack trace, not only the ones whose text a caller cleaned.
 *
 * <p>It swaps the encoder in code, in place of naming a converter class in {@code logback.xml}: the engine is also built as
 * a native image, where a class logback instantiates by name from its configuration needs a reflection entry that only a
 * native build can show missing, and a wrong one would break every log line.
 *
 * <p><b>It never stops the engine.</b> An encoder that does not start is not swapped in, and the appender keeps the encoder it
 * had; any failure, of one appender or of the whole install, is logged and the engine goes on. The appenders an async wrapper
 * holds are reached and swapped too.
 *
 * <p><b>What it leaves unmasked, it says.</b> It logs one {@code event=log_masking_installed masking=N unmasked=M} line, and a
 * warning {@code event=log_masking_unmasked} naming every appender it could not mask: one that is not an output-stream
 * appender, one whose encoder is not a pattern layout, and one that failed to swap.
 *
 * <p><b>When it runs.</b> {@code Main} calls it as soon as the database password is read, on the main thread, before any pool,
 * lock or worker thread exists, so no other thread logs while a console appender is stopped and started for the swap and no
 * event can fall in that window.
 */
public final class LogMasking {

    private static final org.slf4j.Logger log = LoggerFactory.getLogger(LogMasking.class);

    /** How deep an appender-holding appender (an async wrapper) is followed. */
    private static final int MAX_DEPTH = 4;

    private LogMasking() { /* static */ }

    /**
     * {@link #install(LoggerContext)} on the context the engine logs through.
     *
     * @return how many appenders now mask; 0 when the logging backend is not logback
     */
    public static int install() {
        ILoggerFactory factory = LoggerFactory.getILoggerFactory();
        if (factory instanceof LoggerContext context) {
            return install(context);
        }
        log.warn("event=log_masking_unmasked appenders=\"all\" reason=\"the logging backend is not logback\"");
        return 0;
    }

    /**
     * @return how many appenders now mask
     */
    public static int install(LoggerContext context) {
        return install(context, MaskingPatternLayoutEncoder::new);
    }

    /** As {@link #install(LoggerContext)}, with the encoder each swap uses; a test supplies one that does not start. */
    static int install(LoggerContext context, Supplier<MaskingPatternLayoutEncoder> encoders) {
        List<OutputStreamAppender<ILoggingEvent>> todo = new ArrayList<>();
        List<String> unmasked = new ArrayList<>();
        try {
            for (Logger logger : context.getLoggerList()) {
                collect(logger.iteratorForAppenders(), todo, unmasked, 0);
            }
        } catch (Exception | LinkageError e) {
            warn("the appenders could not be listed", null, e);
            return 0;
        }
        int masking = 0;
        for (OutputStreamAppender<ILoggingEvent> appender : todo) {
            try {
                if (appender.getEncoder() instanceof MaskingPatternLayoutEncoder) {
                    masking++;
                } else if (appender.getEncoder() instanceof PatternLayoutEncoder plain
                        && swap(context, appender, plain, encoders)) {
                    masking++;
                } else {
                    unmasked.add(describe(appender));
                }
            } catch (Exception | LinkageError e) {
                unmasked.add(describe(appender));
                warn("an appender could not be swapped", appender.getName(), e);
            }
        }
        log.info("event=log_masking_installed masking={} unmasked={}", masking, unmasked.size());
        if (!unmasked.isEmpty()) {
            log.warn("event=log_masking_unmasked appenders=\"{}\" reason=\"not a pattern-layout output-stream appender, or its "
                + "encoder could not be swapped: what it writes is not masked\"", String.join(", ", unmasked));
        }
        return masking;
    }

    /** The output-stream appenders at or below {@code appenders}, an async wrapper's included; the rest go to {@code unmasked}. */
    private static void collect(Iterator<Appender<ILoggingEvent>> appenders, List<OutputStreamAppender<ILoggingEvent>> todo,
                                List<String> unmasked, int depth) {
        while (appenders.hasNext()) {
            Appender<ILoggingEvent> appender = appenders.next();
            if (appender instanceof OutputStreamAppender<ILoggingEvent> osa) {
                if (!todo.contains(osa)) {
                    todo.add(osa);
                }
            } else if (appender instanceof AppenderAttachable<?> && depth < MAX_DEPTH) {
                @SuppressWarnings("unchecked")
                var inner = ((AppenderAttachable<ILoggingEvent>) appender).iteratorForAppenders();
                collect(inner, todo, unmasked, depth + 1);   // an async wrapper's own appenders are the ones that write
            } else if (!unmasked.contains(describe(appender))) {
                unmasked.add(describe(appender));
            }
        }
    }

    private static String describe(Appender<ILoggingEvent> appender) {
        return appender.getClass().getSimpleName() + "[" + appender.getName() + "]";
    }

    /** @return whether the appender now masks; false leaves it exactly as it was */
    private static boolean swap(LoggerContext context, OutputStreamAppender<ILoggingEvent> appender, PatternLayoutEncoder plain,
                                Supplier<MaskingPatternLayoutEncoder> encoders) {
        MaskingPatternLayoutEncoder masked = encoders.get();
        masked.setContext(context);
        masked.setPattern(plain.getPattern());
        masked.setCharset(plain.getCharset());
        masked.setOutputPatternAsHeader(plain.isOutputPatternAsHeader());
        masked.start();
        if (!masked.isStarted()) {
            warn("the masking encoder did not start; the appender keeps its encoder", appender.getName(), null);
            return false;
        }
        if (appender instanceof ConsoleAppender) {
            // stopped and started so the swap is published to the threads that log; a console appender opens its stream again
            // by itself. Nothing else logs yet (see the class comment), so no event falls between the two calls.
            appender.stop();
            appender.setEncoder(masked);
            appender.start();
            if (!appender.isStarted()) {
                // the appender did not come back: give it the encoder it had and start it again, so it keeps logging
                appender.setEncoder(plain);
                appender.start();
                warn("the console appender did not restart with the masking encoder; it has its old encoder", appender.getName(),
                    null);
                return false;
            }
        } else {
            // a stream the appender was handed is closed by stop and not given back, so the encoder is set as is
            appender.setEncoder(masked);
        }
        return true;
    }

    private static void warn(String what, String appender, Throwable cause) {
        log.warn("event=log_masking_failed what=\"{}\" appender={} cause=\"{}\"", what, appender == null ? "-" : appender,
            cause == null ? "-" : SecretMask.mask(cause.toString()));
    }
}
