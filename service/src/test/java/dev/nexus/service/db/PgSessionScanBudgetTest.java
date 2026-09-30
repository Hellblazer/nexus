/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-wbfpw.47 -- the pure parts of the serving scan budget: the two env overrides
 * (bounded, loud on invalid, the {@code NX_HNSW_EF_SEARCH} precedent) and the
 * work_mem-derived multiplier that holds the per-search memory budget fixed.
 */
class PgSessionScanBudgetTest {

    private static final long MB = 1024L * 1024L;

    @Test
    void maxScanTuples_defaultsTo200000_andAcceptsAnInRangeOverride() {
        assertThat(PgSession.maxScanTuples(null)).isEqualTo(200_000);
        assertThat(PgSession.maxScanTuples("  ")).isEqualTo(200_000);
        assertThat(PgSession.maxScanTuples(" 500000 ")).isEqualTo(500_000);
        assertThat(PgSession.maxScanTuples("1000")).isEqualTo(1_000);
        assertThat(PgSession.maxScanTuples("100000000")).isEqualTo(100_000_000);
    }

    @Test
    void maxScanTuples_failsLoudOnInvalidOrOutOfRange() {
        assertThatThrownBy(() -> PgSession.maxScanTuples("lots"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("NX_HNSW_MAX_SCAN_TUPLES");
        assertThatThrownBy(() -> PgSession.maxScanTuples("999"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("NX_HNSW_MAX_SCAN_TUPLES");
        assertThatThrownBy(() -> PgSession.maxScanTuples("100000001"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("NX_HNSW_MAX_SCAN_TUPLES");
        assertThatThrownBy(() -> PgSession.maxScanTuples("-5"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void scanMemBudgetBytes_defaultsTo16Mb_andBoundsTheOverride() {
        assertThat(PgSession.scanMemBudgetBytes(null)).isEqualTo(16 * MB);
        assertThat(PgSession.scanMemBudgetBytes("32")).isEqualTo(32 * MB);
        assertThat(PgSession.scanMemBudgetBytes("4096")).isEqualTo(4096 * MB);
        assertThatThrownBy(() -> PgSession.scanMemBudgetBytes("0"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("NX_HNSW_SCAN_MEM_BUDGET_MB");
        assertThatThrownBy(() -> PgSession.scanMemBudgetBytes("4097"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("NX_HNSW_SCAN_MEM_BUDGET_MB");
        assertThatThrownBy(() -> PgSession.scanMemBudgetBytes("16MB"))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("NX_HNSW_SCAN_MEM_BUDGET_MB");
    }

    @Test
    void parseWorkMemBytes_readsPostgresUnits() {
        assertThat(PgSession.parseWorkMemBytes("4MB")).isEqualTo(4 * MB);
        assertThat(PgSession.parseWorkMemBytes("384MB")).isEqualTo(384 * MB);
        assertThat(PgSession.parseWorkMemBytes("64kB")).isEqualTo(64 * 1024L);
        assertThat(PgSession.parseWorkMemBytes("1GB")).isEqualTo(1024 * MB);
        assertThatThrownBy(() -> PgSession.parseWorkMemBytes("four megabytes"))
            .isInstanceOf(IllegalStateException.class);
    }

    @Test
    void scanMemMultiplier_holdsTheBudgetFixedAcrossWorkMem() {
        // Local stock work_mem: 16 MB / 4 MB = 4.
        assertThat(PgSession.scanMemMultiplier(4 * MB, 16 * MB)).isEqualTo(4);
        // Managed cloud (384 MB measured): the budget is already exceeded at 1x, so 1.
        assertThat(PgSession.scanMemMultiplier(384 * MB, 16 * MB)).isEqualTo(1);
        // Small work_mem gets a large multiplier, clamped to pgvector's own maximum.
        assertThat(PgSession.scanMemMultiplier(64 * 1024L, 16 * MB)).isEqualTo(256);
        assertThat(PgSession.scanMemMultiplier(1024L, 16 * MB)).isEqualTo(1000);
        // Floor division, never below 1.
        assertThat(PgSession.scanMemMultiplier(5 * MB, 16 * MB)).isEqualTo(3);
        assertThat(PgSession.scanMemMultiplier(4 * MB, 1 * MB)).isEqualTo(1);
        assertThatThrownBy(() -> PgSession.scanMemMultiplier(0, 16 * MB))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
