// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.encoder.PatternLayoutEncoder;
import ch.qos.logback.core.OutputStreamAppender;
import ch.qos.logback.classic.spi.ILoggingEvent;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;

/**
 * nexus-f3yqd.2.18: the log encoder masks a line, its stack trace and its causes, through the appender, and
 * {@link LogMasking#install} puts it on the appenders a configuration already built. Each test has a logging context of its
 * own, so nothing here reaches the JVM's.
 */
class LogMaskingTest {

    private static final String PW = "Hunter2-s3cret";
    private static final String PATTERN = "%level %logger{20} [%X{tenant:--}] - %msg%n";

    private final LoggerContext context = new LoggerContext();
    private final ByteArrayOutputStream out = new ByteArrayOutputStream();

    @BeforeEach
    void secret() {
        context.setMDCAdapter(org.slf4j.MDC.getMDCAdapter());   // a context of its own has none, and its events need one
        context.start();
        SecretMask.add(PW);
    }

    @AfterEach
    void forget() {
        SecretMask.reset();
        context.stop();
    }

    private OutputStreamAppender<ILoggingEvent> appender(PatternLayoutEncoder encoder) {
        encoder.setContext(context);
        encoder.setPattern(PATTERN);
        encoder.start();
        var appender = new OutputStreamAppender<ILoggingEvent>();
        appender.setName("test");
        appender.setContext(context);
        appender.setEncoder(encoder);
        appender.setOutputStream(out);
        appender.start();
        Logger logger = context.getLogger("dev.nexus.test");
        logger.setAdditive(false);
        logger.addAppender(appender);
        return appender;
    }

    private String logged() {
        String text = out.toString(StandardCharsets.UTF_8);
        if (text.isEmpty()) {
            ch.qos.logback.core.util.StatusPrinter.print(context);   // say why nothing was written
        }
        return text;
    }

    private static Throwable trace() {
        var inner = new IllegalStateException("inner: connect to jdbc:postgresql://db/nexus?password=" + PW);
        var outer = new RuntimeException("outer: password=" + PW.toUpperCase(), inner);
        outer.addSuppressed(new IllegalArgumentException("suppressed: " + PW));
        return outer;
    }

    @Test
    void aLineAndItsStackTraceCauseAndSuppressed_leaveNoSecret_andKeepTheirFrames() {
        appender(new MaskingPatternLayoutEncoder());

        context.getLogger("dev.nexus.test").error("event=x cause=\"{}\" password was {}", "text " + PW, PW, trace());

        String text = logged();
        assertThat(text).doesNotContainIgnoringCase(PW).contains("<redacted>");
        assertThat(text).startsWith("ERROR dev.nexus.test [-] - event=x cause=\"text <redacted>\" password was <redacted>");
        assertThat(text).contains("java.lang.RuntimeException: outer: password=<redacted>")
            .contains("Caused by: java.lang.IllegalStateException: inner: connect to jdbc:postgresql://db/nexus?password=<redacted>")
            .contains("Suppressed: java.lang.IllegalArgumentException: suppressed: <redacted>")
            .as("the frames stay, so the trace is still a trace")
            .contains("at dev.nexus.service.log.LogMaskingTest.trace(LogMaskingTest.java");
    }

    @Test
    void theMdcIsMasked_too() {
        appender(new MaskingPatternLayoutEncoder());
        org.slf4j.MDC.put("tenant", PW);
        try {
            context.getLogger("dev.nexus.test").info("hello");
        } finally {
            org.slf4j.MDC.remove("tenant");
        }

        assertThat(logged()).doesNotContain(PW);
    }

    @Test
    void aLineWithNoSecret_isWrittenUnchanged() {
        appender(new MaskingPatternLayoutEncoder());

        context.getLogger("dev.nexus.test").info("event=data_move_call label=\"m\" run=\"m/acme\" pid={}", 42);

        assertThat(logged()).isEqualTo("INFO dev.nexus.test [-] - event=data_move_call label=\"m\" run=\"m/acme\" pid=42\n");
    }

    @Test
    void installPutsTheMaskingEncoderOnAnAppenderAConfigurationBuilt_keepingItsPattern() {
        var plain = new PatternLayoutEncoder();
        var appender = appender(plain);
        context.getLogger("dev.nexus.test").error("before {}", PW);
        assertThat(logged()).as("control: the plain encoder writes the secret").contains(PW);
        out.reset();

        assertThat(LogMasking.install(context)).isEqualTo(1);
        context.getLogger("dev.nexus.test").error("after {}", PW, trace());

        assertThat(appender.getEncoder()).isInstanceOf(MaskingPatternLayoutEncoder.class);
        String text = logged();
        assertThat(text).doesNotContain(PW).startsWith("ERROR dev.nexus.test [-] - after <redacted>");
        assertThat(text).contains("Caused by: java.lang.IllegalStateException");
        assertThat(LogMasking.install(context)).as("a second install changes nothing").isEqualTo(1);
    }

    @Test
    void aConsoleAppender_isSwappedAndStarted_andItsStackTraceIsMasked() {
        var console = new ch.qos.logback.core.ConsoleAppender<ILoggingEvent>();
        var plain = new PatternLayoutEncoder();
        plain.setContext(context);
        plain.setPattern(PATTERN);
        plain.start();
        console.setContext(context);
        console.setEncoder(plain);
        console.start();
        Logger logger = context.getLogger("dev.nexus.test");
        logger.setAdditive(false);
        logger.addAppender(console);
        java.io.PrintStream original = System.out;
        System.setOut(new java.io.PrintStream(out, true, StandardCharsets.UTF_8));
        try {
            assertThat(LogMasking.install(context)).isEqualTo(1);
            assertThat(console.isStarted()).isTrue();
            logger.error("console {}", PW, trace());
        } finally {
            System.setOut(original);
        }

        assertThat(logged()).doesNotContain(PW).contains("console <redacted>").contains("Caused by: java.lang.IllegalStateException");
    }

    @Test
    void anAppenderThatIsNotAPatternLayout_isLeftAlone() {
        var appender = new OutputStreamAppender<ILoggingEvent>();
        appender.setContext(context);
        var layoutEncoder = new ch.qos.logback.core.encoder.LayoutWrappingEncoder<ILoggingEvent>();
        var layout = new ch.qos.logback.classic.PatternLayout();
        layout.setContext(context);
        layout.setPattern("%msg%n");
        layout.start();
        layoutEncoder.setContext(context);
        layoutEncoder.setLayout(layout);
        layoutEncoder.start();
        appender.setEncoder(layoutEncoder);
        appender.setOutputStream(out);
        appender.start();
        Logger logger = context.getLogger("dev.nexus.test");
        logger.setAdditive(false);
        logger.addAppender(appender);

        assertThat(LogMasking.install(context)).isZero();
        assertThat(appender.getEncoder()).isSameAs(layoutEncoder);
    }
}
