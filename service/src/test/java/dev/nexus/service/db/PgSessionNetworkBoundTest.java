/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-u9zkn -- the pure parts of the per-path read bound: the boot parse of
 * {@code NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS}, the statement-bound-to-network-timeout arithmetic and the
 * stamp-bound floor. The behaviour on a live connection is {@code PgNetworkBoundIntegrationTest}; the
 * {@code tcpKeepAlive} pool property is {@code PoolKeepAliveTest}.
 */
class PgSessionNetworkBoundTest {

    @Test
    void theMarginDefaultsToThirtySeconds_whenUnsetOrBlank() {
        assertThat(PgSession.networkBoundMarginMs(null)).isEqualTo(30_000);
        assertThat(PgSession.networkBoundMarginMs("  ")).isEqualTo(30_000);
        assertThat(PgSession.DEFAULT_NETWORK_BOUND_MARGIN_SECONDS).isEqualTo(30);
    }

    @Test
    void zeroDisablesAndInRangeValuesAreTaken() {
        assertThat(PgSession.networkBoundMarginMs("0")).isZero();
        assertThat(PgSession.networkBoundMarginMs(" 5 ")).isEqualTo(5_000);
        assertThat(PgSession.networkBoundMarginMs(Integer.toString(PgSession.NETWORK_BOUND_MARGIN_MAX_SECONDS)))
            .isEqualTo(PgSession.NETWORK_BOUND_MARGIN_MAX_SECONDS * 1000);
    }

    @Test
    void garbageNegativeAndOverflowingValuesAreRefusedLoudly_namingTheVariable() {
        for (String bad : new String[] {"lots", "30s", "1.5", "-1", "3601", "99999999999"}) {
            assertThatThrownBy(() -> PgSession.networkBoundMarginMs(bad))
                .as("value %s", bad)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS");
        }
    }

    @Test
    void theNetworkTimeoutIsTheStatementBoundPlusTheMargin() {
        assertThat(PgSession.networkTimeoutMs(30_000, 30_000)).isEqualTo(60_000);
        assertThat(PgSession.networkTimeoutMs(25_000, 30_000)).isEqualTo(55_000);
        assertThat(PgSession.networkTimeoutMs(1, 1)).isEqualTo(2);
    }

    @Test
    void noStatementBoundOrNoMarginMeansNoNetworkTimeout_andTheSumNeverOverflows() {
        assertThat(PgSession.networkTimeoutMs(0, 30_000)).as("statement_timeout=0 is Postgres for unbounded").isZero();
        assertThat(PgSession.networkTimeoutMs(-1, 30_000)).isZero();
        assertThat(PgSession.networkTimeoutMs(30_000, 0)).as("margin 0 disables the bound").isZero();
        assertThat(PgSession.networkTimeoutMs(Long.MAX_VALUE / 2, 30_000)).isEqualTo(Integer.MAX_VALUE);
        assertThat(PgSession.networkTimeoutMs(Integer.MAX_VALUE, 30_000)).isEqualTo(Integer.MAX_VALUE);
    }

    @Test
    void theStampBoundIsTheMarginWithAFiveSecondFloor_andZeroWhenDisabled() {
        assertThat(PgSession.STAMP_NETWORK_BOUND_FLOOR_MS).isEqualTo(5_000);
        assertThat(PgSession.stampNetworkTimeoutMs(1_000))
            .as("the smallest accepted margin (1 s) must not kill a saturated-but-healthy server's stamp")
            .isEqualTo(5_000);
        assertThat(PgSession.stampNetworkTimeoutMs(4_999)).isEqualTo(5_000);
        assertThat(PgSession.stampNetworkTimeoutMs(5_000)).isEqualTo(5_000);
        assertThat(PgSession.stampNetworkTimeoutMs(30_000)).as("the default margin is above the floor").isEqualTo(30_000);
        assertThat(PgSession.stampNetworkTimeoutMs(0)).as("margin 0 disables the bound, floor included").isZero();
    }
}
