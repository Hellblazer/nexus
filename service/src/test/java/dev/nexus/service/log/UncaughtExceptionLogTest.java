// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.joran.JoranConfigurator;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * nexus-dngtz.1.11: an exception no thread caught goes to the log through the escaping pattern, not to the JVM's stderr
 * handler, which would write a raw line for a message that holds a line break. The event is captured from the engine's real
 * logger and rendered through the SHIPPED {@code logback.xml}.
 */
class UncaughtExceptionLogTest {

    private static final String FORGED =
        "2026-01-01 00:00:00,000 ERROR [main] dev.nexus.service.db.SchemaMigrator [cid=- tenant=- session=-] - "
            + "event=schema_migration_failed";

    private static String renderShipped(ILoggingEvent event) throws Exception {
        var lc = new LoggerContext();
        lc.setMDCAdapter(new ch.qos.logback.classic.util.LogbackMDCAdapter());
        var configurator = new JoranConfigurator();
        configurator.setContext(lc);
        configurator.doConfigure(UncaughtExceptionLogTest.class.getResource("/logback.xml"));
        var appender = (ch.qos.logback.core.OutputStreamAppender<?>) lc.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)
            .getAppender("STDOUT");
        @SuppressWarnings("unchecked")
        var encoder = (PatternLayoutEncoder) ((ch.qos.logback.core.OutputStreamAppender<ILoggingEvent>) appender).getEncoder();
        return new String(encoder.encode(event), StandardCharsets.UTF_8);
    }

    @Test
    void aThreadThatThrowsAForgedMessage_isLoggedAsOneEscapedLine_andItsTraceKeepsItsFrames() throws Exception {
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(UncaughtExceptionLog.class);
        var captured = new ListAppender<ILoggingEvent>();
        captured.start();
        logger.addAppender(captured);
        var before = Thread.getDefaultUncaughtExceptionHandler();
        try {
            UncaughtExceptionLog.install();
            var t = new Thread(() -> {
                throw new IllegalStateException("boom\n" + FORGED, new RuntimeException("cause\r\n" + FORGED));
            }, "worker 1");
            t.start();
            t.join();
        } finally {
            Thread.setDefaultUncaughtExceptionHandler(before);
            logger.detachAppender(captured);
        }

        assertThat(captured.list).as("the thread's death was logged once, through the engine's logger").hasSize(1);
        ILoggingEvent event = captured.list.get(0);
        assertThat(event.getLevel()).isEqualTo(Level.ERROR);
        String out = renderShipped(event);
        List<String> lines = out.lines().toList();
        assertThat(lines.get(0)).contains("] - event=uncaught_exception thread=worker\\x201 exception=java.lang.IllegalStateException");
        assertThat(lines.get(0)).as("the exception class only, not its message").doesNotContain("boom");
        assertThat(lines.get(1)).isEqualTo("java.lang.IllegalStateException: boom\\x0a" + FORGED);
        assertThat(out).contains("Caused by: java.lang.RuntimeException: cause\\x0d\\x0a" + FORGED);
        assertThat(lines.stream().filter(l -> l.startsWith("2026-01-01")).count()).as("no forged line").isZero();
        assertThat(lines.stream().filter(l -> l.startsWith("\tat ")).count()).as("frames on their own lines").isGreaterThan(0);
    }

    @Test
    void installSetsTheDefaultHandler_andAFailingLoggerDoesNotThrowOutOfIt() {
        var before = Thread.getDefaultUncaughtExceptionHandler();
        try {
            UncaughtExceptionLog.install();
            assertThat(Thread.getDefaultUncaughtExceptionHandler()).isNotNull().isNotSameAs(before);
            // a thread with a null name and a throwable whose accessors throw: nothing escapes the handler
            var hostile = new RuntimeException("x") {
                @Override
                public String getMessage() {
                    throw new IllegalStateException("no message for you");
                }
            };
            UncaughtExceptionLog.report(new Thread(() -> { }, "t"), hostile);
        } finally {
            Thread.setDefaultUncaughtExceptionHandler(before);
        }
    }
}
