/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-4ktfm — serving {@code hnsw.ef_search} sizing (design of record: T2
 * nexus/design-4ktfm-hnsw-crowding-remedy). Pure unit tests for the floor
 * parse and the clamp; the SET-issuing half is pinned structurally by
 * {@link HnswServingGucParityTest} and behaviorally by the readback in
 * {@code CombinedQueryParityIntegrationTest}'s harness family.
 */
class PgSessionEfSearchTest {

    // ── NX_HNSW_EF_SEARCH parse ─────────────────────────────────────────────

    @Test
    void unsetOrBlankEnvYieldsTheDefaultFloor() {
        assertThat(PgSession.efSearchFloor(null)).isEqualTo(PgSession.DEFAULT_EF_SEARCH_FLOOR);
        assertThat(PgSession.efSearchFloor("")).isEqualTo(PgSession.DEFAULT_EF_SEARCH_FLOOR);
        assertThat(PgSession.efSearchFloor("   ")).isEqualTo(PgSession.DEFAULT_EF_SEARCH_FLOOR);
    }

    @Test
    void explicitOverrideParses() {
        assertThat(PgSession.efSearchFloor("400")).isEqualTo(400);
        assertThat(PgSession.efSearchFloor(" 40 ")).isEqualTo(40);
        assertThat(PgSession.efSearchFloor("1000")).isEqualTo(1000);
        assertThat(PgSession.efSearchFloor("1")).isEqualTo(1);
    }

    @Test
    void malformedOverrideFailsLoud() {
        assertThatThrownBy(() -> PgSession.efSearchFloor("fast"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("NX_HNSW_EF_SEARCH");
    }

    @Test
    void outOfRangeOverrideFailsLoud() {
        // 0/negative would silently regress recall below even pgvector's
        // default; >1000 would be rejected by Postgres at query time. Both
        // are config errors that must surface at the source.
        assertThatThrownBy(() -> PgSession.efSearchFloor("0"))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PgSession.efSearchFloor("-5"))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PgSession.efSearchFloor("1001"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("1..1000");
    }

    // ── sizing clamp ────────────────────────────────────────────────────────

    @Test
    void floorDominatesSmallRequests() {
        // The common case: nResults=10 must still get the crowd-out headroom.
        assertThat(PgSession.efSearchFor(10, 200)).isEqualTo(200);
        assertThat(PgSession.efSearchFor(1, 200)).isEqualTo(200);
    }

    @Test
    void largeRequestsRiseAboveTheFloor() {
        // MAX_QUERY_RESULTS on the client is 300 — a 300-row request needs at
        // least 300 candidates or HNSW cannot even fill the limit pre-filter.
        assertThat(PgSession.efSearchFor(300, 200)).isEqualTo(300);
    }

    @Test
    void clampAtPgvectorBound() {
        assertThat(PgSession.efSearchFor(5000, 200)).isEqualTo(1000);
        assertThat(PgSession.efSearchFor(10, 1000)).isEqualTo(1000);
    }

    @Test
    void degenerateInputsStayInPgvectorRange() {
        // A zero/negative nResults (caller-validated elsewhere) must never
        // produce a GUC value Postgres rejects.
        assertThat(PgSession.efSearchFor(0, 1)).isEqualTo(1);
        assertThat(PgSession.efSearchFor(-3, 1)).isEqualTo(1);
    }

    // ── NX_SEARCH_STATEMENT_TIMEOUT_MS parse (nexus-g17tf) ──────────────────

    @Test
    void unsetTimeoutYieldsTheEdgeSizedDefault() {
        assertThat(PgSession.searchStatementTimeoutMs(null))
            .isEqualTo(PgSession.DEFAULT_SEARCH_STATEMENT_TIMEOUT_MS)
            .isEqualTo(30_000);
        assertThat(PgSession.searchStatementTimeoutMs(" ")).isEqualTo(30_000);
    }

    @Test
    void timeoutOverrideParses() {
        assertThat(PgSession.searchStatementTimeoutMs(" 5000 ")).isEqualTo(5000);
        assertThat(PgSession.searchStatementTimeoutMs("1")).isEqualTo(1);
    }

    @Test
    void zeroTimeoutIsRefusedBecauseItDisablesTheBound() {
        assertThatThrownBy(() -> PgSession.searchStatementTimeoutMs("0"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("DISABLE");
        assertThatThrownBy(() -> PgSession.searchStatementTimeoutMs("-1"))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PgSession.searchStatementTimeoutMs("600001"))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PgSession.searchStatementTimeoutMs("30s"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("NX_SEARCH_STATEMENT_TIMEOUT_MS");
    }
}
