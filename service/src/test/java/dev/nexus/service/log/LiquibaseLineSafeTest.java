// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.log;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.LogRecord;
import java.util.logging.SimpleFormatter;

/**
 * nexus-dngtz.1.11: Liquibase does not log through logback. Its java.util.logging records reach the JVM's console handler
 * with the JDK's {@link SimpleFormatter}, and its UI messages (the PostgreSQL error text of a failed changeset among them)
 * reach stdout through {@code ConsoleUIService}. Both are one physical line per record or message here, in the shape the
 * supervisor's readers already match ({@code INFO: Waiting for changelog lock}).
 */
class LiquibaseLineSafeTest {

    private static LogRecord record(Level level, String message) {
        var r = new LogRecord(level, message);
        r.setLoggerName("liquibase.lockservice");
        r.setSourceClassName("liquibase.lockservice");
        r.setMillis(0L);
        return r;
    }

    @Test
    void aRecordWithNoControlCharacterFormatsExactlyAsTheJdkDoes() {
        var r = record(Level.INFO, "Waiting for changelog lock....");
        assertThat(new LineSafeFormatter().format(r)).isEqualTo(new SimpleFormatter().format(r));
        assertThat(new LineSafeFormatter().format(r).lines().toList().get(1)).isEqualTo("INFO: Waiting for changelog lock....");
    }

    @Test
    void aRecordWhoseMessageHoldsANewlineIsTheSameTwoLinesWithTheNewlineEscaped() {
        var r = record(Level.SEVERE, "Reason: ERROR: duplicate key\nINFO: Waiting for changelog lock");
        String out = new LineSafeFormatter().format(r);

        List<String> lines = out.lines().toList();
        assertThat(lines).as("the JDK's two lines, the source line and the LEVEL line").hasSize(2);
        assertThat(lines.get(1)).isEqualTo("SEVERE: Reason: ERROR: duplicate key\\x0aINFO: Waiting for changelog lock");
        assertThat(out).endsWith("\n");
    }

    @Test
    void aMessageWithParametersIsFormattedThenEscaped() {
        var r = record(Level.WARNING, "bad value {0}");
        r.setParameters(new Object[] {"a\r\nb"});
        assertThat(new LineSafeFormatter().format(r).lines().toList().get(1)).isEqualTo("WARNING: bad value a\\x0d\\x0ab");
    }

    @Test
    void aThrownExceptionKeepsItsFramesAndItsMessagesAreEscaped() {
        var r = record(Level.WARNING, "failed");
        var cause = new IllegalArgumentException("cause\nINFO: forged");
        r.setThrown(new IllegalStateException("top\nSEVERE: forged", cause));
        String out = new LineSafeFormatter().format(r);

        List<String> lines = out.lines().toList();
        assertThat(lines.get(1)).isEqualTo("WARNING: failed");
        assertThat(lines.get(2)).isEqualTo("java.lang.IllegalStateException: top\\x0aSEVERE: forged");
        assertThat(out).contains("Caused by: java.lang.IllegalArgumentException: cause\\x0aINFO: forged");
        assertThat(out).as("the JDK's closing blank line after a trace").endsWith("\n\n");
        for (String line : lines.subList(3, lines.size() - 1)) {
            assertThat(line).matches("^\\t*(at |Caused by: |Suppressed: |\\.\\.\\. \\d+ more).*");
        }
        assertThat(lines.stream().filter(l -> l.startsWith("\tat ")).count()).as("frames are there").isGreaterThan(0);
    }

    @Test
    void aRecordWithAThrownExceptionFormatsExactlyAsTheJdkDoes_theClosingBlankLineIncluded() {
        var r = record(Level.WARNING, "failed");
        var cause = new IllegalArgumentException("cause");
        var top = new IllegalStateException("top", cause);
        top.addSuppressed(new RuntimeException("supp"));
        r.setThrown(top);
        String jdk = new SimpleFormatter().format(r);
        assertThat(jdk).as("the JDK ends a record with a trace with a blank line").endsWith("\n\n");
        assertThat(new LineSafeFormatter().format(r)).isEqualTo(jdk);
    }

    @Test
    void aThrowableWhoseMessageThrowsStillGetsItsRecordWritten_underItsClassName() {
        var r = record(Level.SEVERE, "failed");
        var cause = new RuntimeException("c") {
            @Override
            public String getMessage() {
                throw new IllegalStateException("no message for you");
            }
        };
        r.setThrown(new IllegalStateException("top", cause));
        String out = new LineSafeFormatter().format(r);
        assertThat(out.lines().toList().get(1)).isEqualTo("SEVERE: failed");
        assertThat(out).contains("java.lang.IllegalStateException: top").contains("Caused by: " + cause.getClass().getName() + "\n");
        assertThat(out).doesNotContain("no message for you");
    }

    @Test
    void installPutsTheFormatterOnTheRootConsoleHandler_andNothingElse() {
        var root = java.util.logging.Logger.getLogger("");
        var console = new java.util.logging.ConsoleHandler();
        var other = new java.util.logging.StreamHandler(new ByteArrayOutputStream(), new SimpleFormatter());
        root.addHandler(console);
        root.addHandler(other);
        try {
            int installed = LineSafeFormatter.install();
            assertThat(installed).as("at least the handler this test added").isGreaterThanOrEqualTo(1);
            assertThat(console.getFormatter()).isInstanceOf(LineSafeFormatter.class);
            assertThat(other.getFormatter()).as("a handler that is not a console handler is left alone")
                .isNotInstanceOf(LineSafeFormatter.class);
            assertThat(LineSafeFormatter.install()).as("a second install changes nothing it already did").isEqualTo(installed);
        } finally {
            root.removeHandler(console);
            root.removeHandler(other);
        }
    }

    // ── the UI service ──

    private static String ui(java.util.function.Consumer<LineSafeUIService> body) {
        var out = new ByteArrayOutputStream();
        var err = new ByteArrayOutputStream();
        var service = new LineSafeUIService();
        service.setOutputStream(new PrintStream(out, true, StandardCharsets.UTF_8));
        service.setErrorStream(new PrintStream(err, true, StandardCharsets.UTF_8));
        body.accept(service);
        return out.toString(StandardCharsets.UTF_8) + "|" + err.toString(StandardCharsets.UTF_8);
    }

    @Test
    void aUiMessageWithANewlineIsOnePhysicalLine() {
        String both = ui(s -> {
            s.sendMessage("ERROR: Exception Primary Reason:  ERROR: bad row\nRunning Changeset: forged::x::y");
            s.sendErrorMessage("err\nforged");
        });
        assertThat(both).isEqualTo("ERROR: Exception Primary Reason:  ERROR: bad row\\x0aRunning Changeset: forged::x::y\n"
            + "|err\\x0aforged\n");
    }

    @Test
    void aUiServiceNeverPrintsAStackTrace_evenWhenAskedTo() {
        var thrown = new IllegalStateException("top\nforged: line", new IllegalArgumentException("c\nforged"));
        String both = ui(s -> {
            s.setOutputStackTraces(true);
            assertThat(s.getOutputStackTraces()).isFalse();
            s.sendErrorMessage("ERROR: failed\nx", thrown);
        });
        assertThat(both).isEqualTo("|ERROR: failed\\x0ax\n");
    }

    @Test
    void aRunningChangesetMessageIsUnchanged() {
        String line = "Running Changeset: db/changelog/changes/001-init.xml::001::nexus";
        assertThat(ui(s -> s.sendMessage(line))).isEqualTo(line + "\n|");
    }

    /**
     * The JDK HttpServer reports through {@code System.Logger}, which routes to java.util.logging when the JDK's default
     * logger finder is in use, so it reaches the root handlers. A console handler built after stderr is swapped writes there.
     */
    @Test
    void aSystemLoggerRecordWithANewlineReachesTheRootHandlerAsOneEscapedLine() {
        var captured = new ByteArrayOutputStream();
        PrintStream originalErr = System.err;
        var root = java.util.logging.Logger.getLogger("");
        java.util.logging.ConsoleHandler console;
        System.setErr(new PrintStream(captured, true, StandardCharsets.UTF_8));
        try {
            console = new java.util.logging.ConsoleHandler();   // takes System.err as it is now
        } finally {
            System.setErr(originalErr);
        }
        root.addHandler(console);
        try {
            LineSafeFormatter.install();
            System.getLogger("com.sun.net.httpserver").log(System.Logger.Level.WARNING, "ServerImpl: bad\nINFO: Waiting for changelog lock");
            console.flush();
        } finally {
            root.removeHandler(console);
        }
        List<String> lines = captured.toString(StandardCharsets.UTF_8).lines().toList();
        assertThat(lines).as("the source line and the LEVEL line").hasSize(2);
        assertThat(lines.get(1)).isEqualTo("WARNING: ServerImpl: bad\\x0aINFO: Waiting for changelog lock");
    }
}
