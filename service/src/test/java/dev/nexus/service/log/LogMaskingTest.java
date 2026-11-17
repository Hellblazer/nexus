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
import java.util.List;

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
    void anEncoderThatDoesNotStart_isNotSwappedIn_andTheAppenderKeepsLogging() {
        var plain = new PatternLayoutEncoder();
        var appender = appender(plain);
        var failing = new MaskingPatternLayoutEncoder() {
            @Override
            public void start() { /* never starts */ }
        };

        assertThat(LogMasking.install(context, () -> failing)).isZero();

        assertThat(appender.getEncoder()).isSameAs(plain);
        assertThat(appender.isStarted()).isTrue();
        context.getLogger("dev.nexus.test").info("still {}", "here");
        assertThat(logged()).isEqualTo("INFO dev.nexus.test [-] - still here\n");
    }

    @Test
    void anEncoderThatThrows_leavesTheAppenderAsItWas_andTheInstallReturns() {
        var plain = new PatternLayoutEncoder();
        var appender = appender(plain);

        assertThat(LogMasking.install(context, () -> {
            throw new IllegalStateException("no encoder for you, password=" + PW);
        })).isZero();

        assertThat(appender.getEncoder()).isSameAs(plain);
        context.getLogger("dev.nexus.test").info("still here");
        assertThat(logged()).contains("still here");
    }

    @Test
    void aConsoleAppenderWhoseEncoderDoesNotStart_keepsLogging() {
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
            assertThat(LogMasking.install(context, () -> new MaskingPatternLayoutEncoder() {
                @Override
                public void start() { /* never starts */ }
            })).isZero();
            logger.info("console still logging");
        } finally {
            System.setOut(original);
        }

        assertThat(console.getEncoder()).isSameAs(plain);
        assertThat(console.isStarted()).isTrue();
        assertThat(logged()).contains("console still logging");
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

        List<String> warnings = installWarnings(() -> assertThat(LogMasking.install(context)).isZero());
        assertThat(appender.getEncoder()).isSameAs(layoutEncoder);
        assertThat(warnings).as("it says what it left unmasked").singleElement().satisfies(w -> assertThat(w)
            .contains("event=log_masking_unmasked").contains("OutputStreamAppender["));
    }

    /** The WARN lines {@link LogMasking} writes while {@code body} runs. */
    private static List<String> installWarnings(Runnable body) {
        Logger logger = (Logger) org.slf4j.LoggerFactory.getLogger(LogMasking.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        try {
            body.run();
            return logs.list.stream().filter(e -> e.getLevel() == ch.qos.logback.classic.Level.WARN)
                .map(ILoggingEvent::getFormattedMessage).toList();
        } finally {
            logger.detachAppender(logs);
            logs.stop();
        }
    }

    @Test
    void anAppenderThatIsNotAnOutputStreamAppender_isNamedInTheUnmaskedWarning() {
        var other = new ch.qos.logback.core.read.ListAppender<ILoggingEvent>();
        other.setName("audit");
        other.setContext(context);
        other.start();
        Logger logger = context.getLogger("dev.nexus.test");
        logger.setAdditive(false);
        logger.addAppender(other);

        List<String> warnings = installWarnings(() -> assertThat(LogMasking.install(context)).isZero());

        assertThat(warnings).singleElement().satisfies(w -> assertThat(w).contains("event=log_masking_unmasked")
            .contains("ListAppender[audit]"));
    }

    @Test
    void anEncoderThatDoesNotStart_isNamedInTheUnmaskedWarning() {
        var plain = new PatternLayoutEncoder();
        var appender = appender(plain);
        appender.setName("console-like");

        List<String> warnings = installWarnings(() -> assertThat(LogMasking.install(context, () -> new MaskingPatternLayoutEncoder() {
            @Override
            public void start() { /* never starts */ }
        })).isZero());

        assertThat(warnings).anySatisfy(w -> assertThat(w).contains("event=log_masking_unmasked").contains("console-like"));
    }

    @Test
    void aPatternAppenderAnAsyncWrapperHolds_isMasked_andNothingIsWarnedAbout() {
        var plain = new PatternLayoutEncoder();
        plain.setContext(context);
        plain.setPattern(PATTERN);
        plain.start();
        var inner = new OutputStreamAppender<ILoggingEvent>();
        inner.setName("inner");
        inner.setContext(context);
        inner.setEncoder(plain);
        inner.setOutputStream(out);
        inner.start();
        var async = new ch.qos.logback.classic.AsyncAppender();
        async.setName("async");
        async.setContext(context);
        async.addAppender(inner);
        async.start();
        Logger logger = context.getLogger("dev.nexus.test");
        logger.setAdditive(false);
        logger.addAppender(async);

        List<String> warnings = installWarnings(() -> assertThat(LogMasking.install(context)).isEqualTo(1));
        logger.error("through the wrapper {}", PW, trace());
        async.stop();   // drains the queue

        assertThat(warnings).isEmpty();
        assertThat(inner.getEncoder()).isInstanceOf(MaskingPatternLayoutEncoder.class);
        assertThat(logged()).doesNotContain(PW).contains("through the wrapper <redacted>");
    }
}
