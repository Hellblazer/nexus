/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-tu8wp.6 -- the boot-time parse of {@code NX_SEARCH_EXACT_MAX_ROWS}, the cardinality router's
 * threshold: default when unset, 0 disables the router, an in-range value is taken, and garbage or an
 * out-of-range value is refused loudly (the {@code NX_HNSW_MAX_SCAN_TUPLES} precedent).
 */
class PgSessionSearchExactMaxRowsTest {

    @Test
    void defaultsToTheMeasuredConstant_whenUnsetOrBlank() {
        assertThat(PgSession.searchExactMaxRows(null)).isEqualTo(PgSession.DEFAULT_SEARCH_EXACT_MAX_ROWS);
        assertThat(PgSession.searchExactMaxRows("   ")).isEqualTo(PgSession.DEFAULT_SEARCH_EXACT_MAX_ROWS);
        // nexus-nqsa7: 30000 (Sam, 2026-10-09). 60000 fixed the 27,893-row repro but put the
        // 45k/58k collections on exact, about +1 s per warm default search on the live engine.
        assertThat(PgSession.DEFAULT_SEARCH_EXACT_MAX_ROWS).isEqualTo(30_000);
    }

    @Test
    void zeroDisablesTheRouter_andInRangeValuesAreTaken() {
        assertThat(PgSession.searchExactMaxRows("0")).isZero();
        assertThat(PgSession.searchExactMaxRows(" 2500 ")).isEqualTo(2_500);
        assertThat(PgSession.searchExactMaxRows("1")).isEqualTo(1);
        assertThat(PgSession.searchExactMaxRows(Integer.toString(PgSession.SEARCH_EXACT_MAX_ROWS_MAX)))
            .isEqualTo(PgSession.SEARCH_EXACT_MAX_ROWS_MAX);
    }

    @Test
    void garbageAndOutOfRangeValuesAreRefusedLoudly_namingTheVariable() {
        for (String bad : new String[] {"lots", "10k", "1.5", "-1", "-5",
                                        Integer.toString(PgSession.SEARCH_EXACT_MAX_ROWS_MAX + 1),
                                        "99999999999"}) {
            assertThatThrownBy(() -> PgSession.searchExactMaxRows(bad))
                .as("value %s", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("NX_SEARCH_EXACT_MAX_ROWS");
        }
    }

    @Test
    void theTestPinOverridesTheEnvValue_andResetRestoresIt() {
        int env = PgSession.searchExactMaxRows();
        try {
            PgSession.overrideSearchExactMaxRowsForTests(7);
            assertThat(PgSession.searchExactMaxRows()).isEqualTo(7);
            PgSession.overrideSearchExactMaxRowsForTests(0);
            assertThat(PgSession.searchExactMaxRows()).isZero();
        } finally {
            PgSession.resetSearchExactMaxRowsForTests();
        }
        assertThat(PgSession.searchExactMaxRows()).isEqualTo(env);
        assertThat(PgSession.startupSearchExactMaxRows()).isEqualTo(env);
    }
}
