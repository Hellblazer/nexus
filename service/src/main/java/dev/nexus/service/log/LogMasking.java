// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.ConsoleAppender;
import ch.qos.logback.core.OutputStreamAppender;
import org.slf4j.ILoggerFactory;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

/**
 * Puts {@link MaskingPatternLayoutEncoder} on every pattern-layout appender of the logging context, so {@link SecretMask}
 * covers every line the engine writes and every stack trace, not only the ones whose text a caller cleaned.
 *
 * <p>It swaps the encoder in code, in place of naming a converter class in {@code logback.xml}: the engine is also built as
 * a native image, where a class logback instantiates by name from its configuration needs a reflection entry that only a
 * native build can show missing, and a wrong one would break every log line. An appender whose encoder is not a
 * {@link PatternLayoutEncoder} is left as it is, and a failure here is logged and never stops the engine.
 */
public final class LogMasking {

    private static final org.slf4j.Logger log = LoggerFactory.getLogger(LogMasking.class);

    private LogMasking() { /* static */ }

    /** {@link #install(LoggerContext)} on the context the engine logs through. */
    public static void install() {
        ILoggerFactory factory = LoggerFactory.getILoggerFactory();
        if (factory instanceof LoggerContext context) {
            install(context);
        } else {
            log.warn("event=log_masking_not_installed reason=\"the logging backend is not logback\"");
        }
    }

    /**
     * @return how many appenders now mask
     */
    public static int install(LoggerContext context) {
        List<OutputStreamAppender<ILoggingEvent>> todo = new ArrayList<>();
        for (Logger logger : context.getLoggerList()) {
            for (Iterator<ch.qos.logback.core.Appender<ILoggingEvent>> it = logger.iteratorForAppenders(); it.hasNext();) {
                var appender = it.next();
                if (appender instanceof OutputStreamAppender<ILoggingEvent> osa && !todo.contains(osa)) {
                    todo.add(osa);
                }
            }
        }
        int masking = 0;
        for (OutputStreamAppender<ILoggingEvent> appender : todo) {
            try {
                if (appender.getEncoder() instanceof MaskingPatternLayoutEncoder) {
                    masking++;
                } else if (appender.getEncoder() instanceof PatternLayoutEncoder plain) {
                    var masked = new MaskingPatternLayoutEncoder();
                    masked.setContext(context);
                    masked.setPattern(plain.getPattern());
                    masked.setCharset(plain.getCharset());
                    masked.setOutputPatternAsHeader(plain.isOutputPatternAsHeader());
                    masked.start();
                    if (appender instanceof ConsoleAppender) {
                        // stopped and started so the swap is published to the threads that log; a console appender opens its
                        // stream again by itself
                        appender.stop();
                        appender.setEncoder(masked);
                        appender.start();
                    } else {
                        // a stream the appender was handed is closed by stop and not given back, so the encoder is set as is:
                        // at boot, before another thread logs, which is when this runs
                        appender.setEncoder(masked);
                    }
                    masking++;
                }
            } catch (RuntimeException e) {
                log.warn("event=log_masking_appender_failed appender={} cause=\"{}\"", appender.getName(),
                    SecretMask.mask(e.toString()));
            }
        }
        return masking;
    }
}
