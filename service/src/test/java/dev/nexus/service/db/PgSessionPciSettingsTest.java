/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-43ulx.10 (RDR-227 Step 2) -- the boot-time parse of the four {@code NX_SEARCH_PCI*} settings
 * and the warning when the build threshold B sits above the router threshold T. Same shape as
 * {@link PgSessionSearchExactMaxRowsTest}: default when unset or blank, every bound and one past it,
 * garbage refused loudly with the variable named.
 */
class PgSessionPciSettingsTest {

    // ---- NX_SEARCH_PCI ---------------------------------------------------------------------

    @Test
    void searchPci_defaultsToOn_whenUnsetOrBlank() {
        assertThat(PgSession.searchPci(null)).isTrue();
        assertThat(PgSession.searchPci("   ")).isTrue();
    }

    @Test
    void searchPci_takesOnlyOneAndZero() {
        assertThat(PgSession.searchPci("1")).isTrue();
        assertThat(PgSession.searchPci(" 1 ")).isTrue();
        assertThat(PgSession.searchPci("0")).isFalse();
        assertThat(PgSession.searchPci(" 0 ")).isFalse();
    }

    @Test
    void searchPci_refusesEverythingElse_namingTheVariable() {
        for (String bad : new String[] {"true", "yes", "TRUE", "on", "2", "-1", "01", "1.0", "off"}) {
            assertThatThrownBy(() -> PgSession.searchPci(bad))
                .as("value %s", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("NX_SEARCH_PCI")
                .hasMessageContaining(bad);
        }
    }

    // ---- NX_SEARCH_PCI_BUILD_MIN_ROWS ------------------------------------------------------

    @Test
    void buildMinRows_defaultsTo20000_whenUnsetOrBlank() {
        assertThat(PgSession.DEFAULT_SEARCH_PCI_BUILD_MIN_ROWS).isEqualTo(20_000);
        assertThat(PgSession.searchPciBuildMinRows(null)).isEqualTo(20_000);
        assertThat(PgSession.searchPciBuildMinRows("  ")).isEqualTo(20_000);
    }

    @Test
    void buildMinRows_takesTheBounds() {
        assertThat(PgSession.searchPciBuildMinRows("1")).isEqualTo(1);
        assertThat(PgSession.searchPciBuildMinRows(" 25000 ")).isEqualTo(25_000);
        assertThat(PgSession.searchPciBuildMinRows("1000000")).isEqualTo(1_000_000);
    }

    @Test
    void buildMinRows_refusesOnePastEachBoundAndGarbage() {
        assertRefused("NX_SEARCH_PCI_BUILD_MIN_ROWS", PgSession::searchPciBuildMinRows,
            "0", "-1", "1000001", "99999999999", "lots", "20k", "1.5");
    }

    // ---- NX_SEARCH_PCI_SWEEP_SECONDS -------------------------------------------------------

    @Test
    void sweepSeconds_defaultsTo600_whenUnsetOrBlank() {
        assertThat(PgSession.DEFAULT_SEARCH_PCI_SWEEP_SECONDS).isEqualTo(600);
        assertThat(PgSession.searchPciSweepSeconds(null)).isEqualTo(600);
        assertThat(PgSession.searchPciSweepSeconds("")).isEqualTo(600);
    }

    @Test
    void sweepSeconds_takesTheBounds() {
        assertThat(PgSession.searchPciSweepSeconds("60")).isEqualTo(60);
        assertThat(PgSession.searchPciSweepSeconds(" 3600 ")).isEqualTo(3_600);
        assertThat(PgSession.searchPciSweepSeconds("86400")).isEqualTo(86_400);
    }

    @Test
    void sweepSeconds_refusesOnePastEachBoundAndGarbage() {
        assertRefused("NX_SEARCH_PCI_SWEEP_SECONDS", PgSession::searchPciSweepSeconds,
            "59", "0", "-60", "86401", "99999999999", "ten minutes", "600s", "1.5");
    }

    // ---- NX_SEARCH_PCI_MAX_PER_LEAF --------------------------------------------------------

    @Test
    void maxPerLeaf_defaultsTo16_whenUnsetOrBlank() {
        assertThat(PgSession.DEFAULT_SEARCH_PCI_MAX_PER_LEAF).isEqualTo(16);
        assertThat(PgSession.searchPciMaxPerLeaf(null)).isEqualTo(16);
        assertThat(PgSession.searchPciMaxPerLeaf(" ")).isEqualTo(16);
    }

    @Test
    void maxPerLeaf_zeroBuildsNone_andTheBoundsAreTaken() {
        assertThat(PgSession.searchPciMaxPerLeaf("0")).isZero();
        assertThat(PgSession.searchPciMaxPerLeaf(" 8 ")).isEqualTo(8);
        assertThat(PgSession.searchPciMaxPerLeaf("1000")).isEqualTo(1_000);
    }

    @Test
    void maxPerLeaf_refusesOnePastEachBoundAndGarbage() {
        assertRefused("NX_SEARCH_PCI_MAX_PER_LEAF", PgSession::searchPciMaxPerLeaf,
            "-1", "1001", "99999999999", "many", "16x", "1.5");
    }

    // ---- the record and the boot touch -----------------------------------------------------

    @Test
    void startupPciSettings_matchesTheEnvResolvedAccessor() {
        PgSession.PciSettings s = PgSession.startupPciSettings();
        assertThat(s).isEqualTo(PgSession.pciSettings());
        assertThat(s.buildMinRows()).isBetween(1, 1_000_000);
        assertThat(s.sweepSeconds()).isBetween(60, 86_400);
        assertThat(s.maxPerLeaf()).isBetween(0, 1_000);
    }

    // ---- the boot log lines and the B > T warning ------------------------------------------

    @Test
    void warnsWhenBuildThresholdIsAboveTheRouterThreshold() {
        List<ILoggingEvent> events = capture(() ->
            PgSession.logPciBootSettings(new PgSession.PciSettings(true, 40_000, 600, 16), 30_000));

        ILoggingEvent warn = single(events, Level.WARN, "event=pci_build_threshold_above_router");
        assertThat(warn.getFormattedMessage())
            .contains("build_min_rows=40000")
            .contains("router_max_rows=30000");
        assertThat(single(events, Level.INFO, "event=pci_settings").getFormattedMessage())
            .contains("enabled=true")
            .contains("build_min_rows=40000")
            .contains("sweep_seconds=600")
            .contains("max_per_leaf=16");
    }

    @Test
    void doesNotWarn_whenBuildThresholdIsAtOrBelowTheRouterThreshold() {
        // B == T: a collection at exactly T+1 rows is routed to HNSW and B == T builds for it.
        for (int b : new int[] {1, 20_000, 30_000}) {
            List<ILoggingEvent> events = capture(() ->
                PgSession.logPciBootSettings(new PgSession.PciSettings(true, b, 600, 16), 30_000));
            assertThat(events).as("b=%d", b).noneMatch(e -> e.getFormattedMessage().contains("pci_build_threshold_above_router"));
            single(events, Level.INFO, "event=pci_settings");
        }
    }

    @Test
    void doesNotWarn_whenTheRouterIsDisabled() {
        // T == 0 sends every statement to HNSW, so no collection is stranded between T and B.
        List<ILoggingEvent> events = capture(() ->
            PgSession.logPciBootSettings(new PgSession.PciSettings(true, 1_000_000, 600, 16), 0));
        assertThat(events).noneMatch(e -> e.getFormattedMessage().contains("pci_build_threshold_above_router"));
        single(events, Level.INFO, "event=pci_settings");
    }

    @Test
    void theWarningDecisionIsExactlyTPositiveAndBAboveT() {
        assertThat(PgSession.pciBuildThresholdAboveRouter(30_001, 30_000)).isTrue();
        assertThat(PgSession.pciBuildThresholdAboveRouter(30_000, 30_000)).isFalse();
        assertThat(PgSession.pciBuildThresholdAboveRouter(5, 0)).isFalse();
        assertThat(PgSession.pciBuildThresholdAboveRouter(1, 1)).isFalse();
        assertThat(PgSession.pciBuildThresholdAboveRouter(2, 1)).isTrue();
    }

    // ---- helpers ---------------------------------------------------------------------------

    private static void assertRefused(String variable, java.util.function.Function<String, Integer> parse,
                                      String... bad) {
        for (String v : bad) {
            assertThatThrownBy(() -> parse.apply(v))
                .as("value %s", v)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(variable);
        }
    }

    private static ILoggingEvent single(List<ILoggingEvent> events, Level level, String marker) {
        List<ILoggingEvent> hits = events.stream()
            .filter(e -> e.getLevel() == level && e.getFormattedMessage().contains(marker))
            .toList();
        assertThat(hits).as("%s %s", level, marker).hasSize(1);
        return hits.get(0);
    }

    private static List<ILoggingEvent> capture(Runnable body) {
        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.run();
            return List.copyOf(logs.list);
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }
}
