// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.classic.joran.JoranConfigurator;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.LoggingEvent;
import ch.qos.logback.core.OutputStreamAppender;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.regex.Pattern;

/**
 * nexus-dngtz.1.11 (RDR-167, Sam's ruling): no value a client chose can start a new physical line in the engine's log. The
 * escape is at the sink, so it covers every log call and every library that logs through logback. These tests render events
 * through the SHIPPED {@code logback.xml}, before and after {@link LogMasking} swaps the encoder.
 */
class LogSinkEscapeTest {

    private static final String PW = "Hunter2-s3cret";
    private static final String FORGED_LINE =
        "2026-01-01 00:00:00,000 ERROR [main] dev.nexus.service.db.SchemaMigrator [cid=- tenant=- session=-] - "
            + "event=schema_migration_failed";
    /** The shape every line of a trace after the first has: a frame, a cause, a suppressed one, or a frame count. */
    private static final Pattern TRACE_LINE =
        Pattern.compile("^\\t*(at |Caused by: |Suppressed: |\\.\\.\\. \\d+ (more|common frames omitted)).*");

    private LoggerContext context;
    private OutputStreamAppender<ILoggingEvent> appender;

    @BeforeEach
    void shipped() throws Exception {
        URL shipped = LogSinkEscapeTest.class.getResource("/logback.xml");
        assertThat(shipped).as("logback.xml on the classpath").isNotNull();
        assertThat(shipped.getPath()).as("the shipped copy, not a test resource").doesNotContain("test-classes");
        context = new LoggerContext();
        context.setMDCAdapter(new ch.qos.logback.classic.util.LogbackMDCAdapter());
        var configurator = new JoranConfigurator();
        configurator.setContext(context);
        configurator.doConfigure(shipped);
        @SuppressWarnings("unchecked")
        var found = (OutputStreamAppender<ILoggingEvent>) context.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME).getAppender("STDOUT");
        appender = found;
        assertThat(appender).isNotNull();
    }

    @AfterEach
    void forget() {
        SecretMask.reset();
        context.stop();
    }

    private LoggingEvent event(String logger, Level level, String message, Throwable thrown, Object... args) {
        Logger l = context.getLogger(logger);
        var e = new LoggingEvent(Logger.class.getName(), l, level, message, thrown, args);
        e.setThreadName("main");
        e.setTimeStamp(0L);
        return e;
    }

    private String render(LoggingEvent e) {
        return new String(((PatternLayoutEncoder) appenderEncoder()).encode(e), StandardCharsets.UTF_8);
    }

    private Object appenderEncoder() {
        return appender.getEncoder();
    }

    private static void assertOnePhysicalLine(String rendered) {
        assertThat(rendered).endsWith("\n");
        assertThat(rendered.substring(0, rendered.length() - 1)).as("no line break inside the line").doesNotContain("\n", "\r",
            "\u0085", "\u2028", "\u2029");
    }

    // ── the two paths: the encoder logback.xml builds, and the one LogMasking swaps in ──

    private interface Renderer {
        String render(LoggingEvent e);
    }

    private void bothPaths(java.util.function.Consumer<Renderer> body) {
        body.accept(this::render);
        SecretMask.add(PW);
        assertThat(LogMasking.install(context)).as("the masking encoder is on the appender").isEqualTo(1);
        assertThat(appender.getEncoder()).isInstanceOf(MaskingPatternLayoutEncoder.class);
        body.accept(e -> new String(((MaskingPatternLayoutEncoder) appender.getEncoder()).encode(e), StandardCharsets.UTF_8));
    }

    @Test
    void aNewlineAndAForgedLineInAMessageArgumentRenderOnOnePhysicalLine() {
        bothPaths(r -> {
            String out = r.render(event("dev.nexus.service.Main", Level.ERROR, "event=x path={}", null, "x\n" + FORGED_LINE));
            assertOnePhysicalLine(out);
            assertThat(out).contains("path=x\\x0a" + FORGED_LINE);
            assertThat(out.lines().count()).isEqualTo(1);
            assertThat(out).doesNotContain("\n2026-");
        });
    }

    @Test
    void aForgedPrefixTerminatorWithoutANewlineStaysOnOneLine_andTheRealEventTokenComesFirst() {
        bothPaths(r -> {
            String out = r.render(event("dev.nexus.service.Main", Level.INFO, "event=real_event v={}", null,
                "\"] - event=schema_migration_failed"));
            assertOnePhysicalLine(out);
            String afterRealPrefix = out.substring(out.indexOf("] - ") + 4);
            assertThat(afterRealPrefix).as("the text after the real prefix starts with the real event token")
                .startsWith("event=real_event ");
        });
    }

    @Test
    void anExceptionWhoseMessageAndCauseMessageHoldForgedLines_rendersEscaped_framesOnTheirOwnLines_andOnce() {
        bothPaths(r -> {
            var cause = new IllegalArgumentException("cause\n" + FORGED_LINE);
            var top = new IllegalStateException("top\r\n" + FORGED_LINE, cause);
            top.addSuppressed(new RuntimeException("supp\u2028" + FORGED_LINE));
            String out = r.render(event("dev.nexus.service.db.SchemaMigrator", Level.ERROR, "event=boom", top));

            List<String> lines = out.lines().toList();
            assertThat(lines.get(0)).contains("] - event=boom");
            assertThat(lines.get(1)).isEqualTo("java.lang.IllegalStateException: top\\x0d\\x0a" + FORGED_LINE);
            assertThat(out).contains("Caused by: java.lang.IllegalArgumentException: cause\\x0a" + FORGED_LINE)
                .contains("Suppressed: java.lang.RuntimeException: supp\\u2028" + FORGED_LINE);
            for (String line : lines.subList(2, lines.size())) {
                assertThat(line).as("every later line is a frame, a cause, a suppressed one or a count")
                    .matches(TRACE_LINE).doesNotStartWith("2026-");
            }
            assertThat(lines.stream().filter(l -> l.contains("\tat dev.nexus.service.log.LogSinkEscapeTest")).count())
                .as("frames are there").isGreaterThan(0);
            assertThat(countOf(out, "java.lang.IllegalStateException: top")).as("the exception is printed exactly once").isEqualTo(1);
            assertThat(countOf(out, "Caused by: java.lang.IllegalArgumentException")).isEqualTo(1);
        });
    }

    private LoggingEvent eventOnThread(String threadName) {
        Logger l = context.getLogger("dev.nexus.service.Main");
        var e = new LoggingEvent(Logger.class.getName(), l, Level.INFO, "event=x", null, new Object[0]);
        e.setThreadName(threadName);
        e.setTimeStamp(0L);
        return e;
    }

    @Test
    void aThreadNameThatHoldsAForgedTerminatorAndANewlineRendersEscapedOnOneLine() {
        bothPaths(r -> {
            String out = r.render(eventOnThread("w] - event=schema_migration_failed\nx"));
            assertOnePhysicalLine(out);
            assertThat(out.lines().count()).isEqualTo(1);
            assertThat(out).contains("[w\\x5d\\x20-\\x20event\\x3dschema_migration_failed\\x0ax] ");
            assertThat(out).contains("] - event=x\n").doesNotContain("] - event=schema_migration_failed");
        });
    }

    @Test
    void aNormalThreadNameAndAnEmptyOneRenderAsBefore() {
        bothPaths(r -> {
            assertThat(r.render(eventOnThread("main"))).contains(" [main] dev.nexus.service.Main ");
            assertThat(r.render(eventOnThread("t1-ttl-sweep"))).contains(" [t1-ttl-sweep] dev.nexus.service.Main ");
            assertThat(r.render(eventOnThread(""))).as("a plain virtual thread has no name").contains(" [] dev.nexus.service.Main ");
        });
    }

    @Test
    void carriageReturnNextLineAndLineSeparatorAreEachEscapedInAMessage() {
        bothPaths(r -> {
            String out = r.render(event("t.X", Level.INFO, "a\rb\u0085c\u2028d\u2029e", null));
            assertOnePhysicalLine(out);
            assertThat(out).contains("a\\x0db\\x85c\\u2028d\\u2029e");
        });
    }

    @Test
    void aNormalLineIsByteIdenticalToTheOneBeforeTheSink() {
        bothPaths(r -> {
            String out = r.render(event("dev.nexus.service.Main", Level.INFO, "event=data_move_call label=\"m\" run={} \\x0a [x] =", null,
                "m/acme"));
            assertThat(out).isEqualTo("1970-01-01 00:00:00,000 INFO  [main] dev.nexus.service.Main "
                + "[cid=- tenant=- session=-] - event=data_move_call label=\"m\" run=m/acme \\x0a [x] =\n");
        });
    }

    @Test
    void aNormalExceptionKeepsLogbacksOwnShape() {
        bothPaths(r -> {
            var cause = new IllegalArgumentException("c");
            var top = new IllegalStateException("t", cause);
            String out = r.render(event("t.X", Level.ERROR, "m", top));
            List<String> lines = out.lines().toList();
            assertThat(lines.get(1)).isEqualTo("java.lang.IllegalStateException: t");
            assertThat(lines.get(2)).startsWith("\tat dev.nexus.service.log.LogSinkEscapeTest.");
            assertThat(out).contains("\nCaused by: java.lang.IllegalArgumentException: c\n");
        });
    }

    @Test
    void anExceptionWithNoMessageRendersLikeLogback() {
        bothPaths(r -> {
            String out = r.render(event("t.X", Level.ERROR, "m", new IllegalStateException()));
            assertThat(out.lines().skip(1).findFirst()).contains("java.lang.IllegalStateException: null");
        });
    }

    @Test
    void afterTheMaskingInstall_theSecretIsStillMasked_inTheMessageAndInTheExceptionMessage() {
        SecretMask.add(PW);
        assertThat(LogMasking.install(context)).isEqualTo(1);
        var masking = (MaskingPatternLayoutEncoder) appender.getEncoder();
        var top = new IllegalStateException("connect failed password=" + PW + "\n" + FORGED_LINE);
        String out = new String(masking.encode(event("t.X", Level.ERROR, "event=x pw={}\n" + FORGED_LINE, top, PW)),
            StandardCharsets.UTF_8);

        assertThat(out).doesNotContain(PW).contains("<redacted>");
        assertThat(out).contains("event=x pw=<redacted>\\x0a" + FORGED_LINE)
            .contains("java.lang.IllegalStateException: connect failed password=<redacted>\\x0a" + FORGED_LINE);
        assertThat(out.lines().filter(l -> l.startsWith("2026-")).count()).as("no forged line").isZero();
    }

    private static int countOf(String text, String part) {
        int n = 0;
        for (int i = text.indexOf(part); i >= 0; i = text.indexOf(part, i + part.length())) {
            n++;
        }
        return n;
    }
}
