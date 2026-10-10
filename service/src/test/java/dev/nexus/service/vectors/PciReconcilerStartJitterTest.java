// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.db.PgSession.PciSettings;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.util.Random;
import java.util.random.RandomGenerator;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.19, fix round): the reconciler's first run is jittered, so engines restarted together
 * (a rolling deploy, a node reboot) do not all open their counting transactions at once. No database.
 */
class PciReconcilerStartJitterTest {

    /** A generator that always draws the top of its range. */
    private static final RandomGenerator TOP = new RandomGenerator() {
        @Override public long nextLong() {
            return Long.MAX_VALUE;
        }

        @Override public long nextLong(long bound) {
            return bound - 1;
        }
    };

    private static final RandomGenerator BOTTOM = new RandomGenerator() {
        @Override public long nextLong() {
            return 0;
        }

        @Override public long nextLong(long bound) {
            return 0;
        }
    };

    @Test
    void theJitter_isAtMostTheShorterOfSixtySecondsAndATenthOfThePeriod() {
        assertThat(PciReconciler.startJitterMillis(Duration.ofSeconds(600), TOP)).isEqualTo(60_000);
        assertThat(PciReconciler.startJitterMillis(Duration.ofSeconds(100), TOP)).isEqualTo(10_000);
        assertThat(PciReconciler.startJitterMillis(Duration.ofSeconds(3600), TOP)).as("capped").isEqualTo(60_000);
        assertThat(PciReconciler.startJitterMillis(Duration.ofSeconds(60), TOP)).isEqualTo(6_000);
        assertThat(PciReconciler.startJitterMillis(Duration.ofMillis(200), TOP)).isEqualTo(20);
        assertThat(PciReconciler.startJitterMillis(Duration.ofMillis(5), TOP)).as("nothing to spread").isZero();
        assertThat(PciReconciler.startJitterMillis(Duration.ofSeconds(600), BOTTOM)).isZero();
    }

    @Test
    void theJitter_isDrawnFromTheInjectedSource_andStaysInRange() {
        Random random = new Random(7);
        long first = PciReconciler.startJitterMillis(Duration.ofSeconds(600), random);
        assertThat(first).isEqualTo(PciReconciler.startJitterMillis(Duration.ofSeconds(600), new Random(7)));
        for (int i = 0; i < 1000; i++) {
            assertThat(PciReconciler.startJitterMillis(Duration.ofSeconds(600), random)).isBetween(0L, 60_000L);
        }
    }

    @Test
    void start_waitsTheJitter_insteadOfRunningAPassAtOnce() {
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PciReconciler.class);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        Level before = logger.getLevel();
        logger.setLevel(Level.INFO);
        logger.addAppender(appender);
        PciSettings s = new PciSettings(true, 20_000, 600, 16);
        var builder = new PciBuilderSession("jdbc:postgresql://127.0.0.1:1/none", "u", "p", "n1", s);
        var reconciler = new PciReconciler(() -> {
            throw new IllegalStateException("no catalog in this test");
        }, builder, new PciIndexSweep(() -> null, s), s, Clock.systemUTC(), PciReconciler.SET_LOCAL_TENANT,
            PciReconciler.COUNT_TIMEOUT, Duration.ofSeconds(600), TOP);
        try {
            reconciler.start();
            assertThat(reconciler.passes()).as("no pass yet: the first run waits").isZero();
            assertThat(appender.list.stream().map(ILoggingEvent::getFormattedMessage))
                .anyMatch(l -> l.contains("event=pci_reconciler_started") && l.contains("start_delay_ms=60000"));
        } finally {
            reconciler.stop();
            logger.detachAppender(appender);
            logger.setLevel(before);
        }
    }
}
