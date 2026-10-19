// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-s9wav: the log line an operator reads after a boot, and the pin that the four texts the Java check
 * compares against are the four the changeset writes and compares against (the changeset carries them twice, in
 * the {@code vectors-034-1} helper and the {@code vectors-034-2} self-heal). The database-backed behaviour is
 * {@code ChunkLeafAutovacuumReloptionsIntegrationTest}.
 */
class ChunksLeafAutovacuumCheckTest {

    private static List<String> logged(Runnable body) {
        Logger logger = (Logger) LoggerFactory.getLogger(ChunksLeafAutovacuumCheck.class);
        ListAppender<ILoggingEvent> appender = new ListAppender<>();
        appender.start();
        logger.addAppender(appender);
        try {
            body.run();
            return appender.list.stream().map(e -> e.getLevel() + " " + e.getFormattedMessage()).toList();
        } finally {
            logger.detachAppender(appender);
            appender.stop();
        }
    }

    @Test
    void aHealthyReportLogsAtInfo() {
        var report = new ChunksLeafAutovacuumCheck.Report(5, List.of(), List.of());
        assertThat(report.healthy()).isTrue();
        assertThat(logged(() -> ChunksLeafAutovacuumCheck.log(report))).containsExactly(
            "INFO event=chunks_leaf_autovacuum_checked leaves=5 policy_unset=0 autovacuum_disabled=0");
    }

    @Test
    void anUnsetOrDisabledLeafLogsAtWarnNamingIt() {
        var report = new ChunksLeafAutovacuumCheck.Report(5, List.of("chunks_a"), List.of("chunks_b", "chunks_c"));
        assertThat(report.healthy()).isFalse();
        List<String> out = logged(() -> ChunksLeafAutovacuumCheck.log(report));
        assertThat(out).hasSize(1);
        assertThat(out.get(0)).startsWith("WARN event=chunks_leaf_autovacuum_checked leaves=5 policy_unset=1 autovacuum_disabled=2")
            .contains("chunks_a").contains("chunks_b").contains("chunks_c");
    }

    @Test
    void noLeafAtAllIsAWarnNotAnInfo() {
        var report = new ChunksLeafAutovacuumCheck.Report(0, List.of(), List.of());
        assertThat(report.healthy()).as("zero leaves is not healthy").isFalse();
        List<String> out = logged(() -> ChunksLeafAutovacuumCheck.log(report));
        assertThat(out).hasSize(1);
        assertThat(out.get(0)).startsWith("WARN ").contains("leaves=0 policy_unset=0 autovacuum_disabled=0");
    }

    @Test
    void theFourTextsTheCheckComparesAgainstAreTheOnesTheChangesetWritesAndComparesTwice() throws Exception {
        assertThat(ChunksLeafAutovacuumCheck.POLICY).containsExactlyInAnyOrder(
            "autovacuum_vacuum_scale_factor=0.05",
            "autovacuum_vacuum_threshold=1000",
            "autovacuum_vacuum_insert_scale_factor=0.05",
            "autovacuum_analyze_scale_factor=0.02");
        String xml;
        try (var in = ChunksLeafAutovacuumCheckTest.class.getClassLoader()
                .getResourceAsStream("db/changelog/vectors-034-chunks-leaf-autovacuum.xml")) {
            assertThat(in).isNotNull();
            xml = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        for (String text : ChunksLeafAutovacuumCheck.POLICY) {
            // the helper (vectors-034-1) and the self-heal (vectors-034-2) each compare against it
            assertThat(xml.split("'" + java.util.regex.Pattern.quote(text) + "'", -1).length - 1)
                .as("quoted occurrences of %s in the changeset", text).isGreaterThanOrEqualTo(2);
        }
    }
}
