// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/**
 * nexus-f3yqd.2.14: the rules of the finish that need no database: which moves are over the chunk family, how the
 * registry's verification text is bound, and the {@code lock_timeout} of {@code on_complete}.
 */
class MoveFinishRulesTest {

    private static DataMove move(String schema, String table, String onComplete) {
        return new DataMove("m", "update <driving_table> set n = 1", schema, table, 100, 0, 60_000, "select 1", onComplete,
            null, null, null, 0, null, null);
    }

    // ── the chunk family ───────────────────────────────────────────────────────────────────────────────────

    @Test
    void aMoveIsChunkFamily_whenTheDrivingTableIs_orOnCompleteNamesAMember() {
        assertThat(ChunkFamily.touches(move("nexus", "chunks", null))).isTrue();
        assertThat(ChunkFamily.touches(move("nexus", "chunks_m1_default", null))).as("a leaf").isTrue();
        assertThat(ChunkFamily.touches(move("nexus", "catalog_document_chunks", null))).isTrue();
        assertThat(ChunkFamily.touches(move("nexus", "topic_assignments", null))).isTrue();
        assertThat(ChunkFamily.touches(move("nexus", "chunk_orphaned_at", null))).isTrue();
        assertThat(ChunkFamily.touches(move("dm_test", "plain_rows", "alter table nexus.chunks validate constraint c")))
            .as("on_complete names a member").isTrue();
        assertThat(ChunkFamily.touches(move("dm_test", "plain_rows", "alter table \"NEXUS\" . \"Chunks\" drop column x")))
            .as("quotes, case and spaces around the dot do not hide it").isTrue();
        assertThat(ChunkFamily.touches(move("dm_test", "plain_rows", "alter table chunks_m2_acme drop column x")))
            .as("a leaf named without a schema").isTrue();
        assertThat(ChunkFamily.touches(move("dm_test", "plain_rows", "alter table x drop column topic_assignments")))
            .as("a name in the text counts (a false positive costs one ended build)").isTrue();
    }

    @Test
    void aMoveOutsideTheFamily_isNot() {
        assertThat(ChunkFamily.touches(move("nexus", "memory", null))).isFalse();
        assertThat(ChunkFamily.touches(move("dm_test", "chunks", null))).as("another schema's chunks").isFalse();
        assertThat(ChunkFamily.touches(move("nexus", "plain_rows", "alter table nexus.catalog_documents validate constraint c")))
            .as("catalog_documents is not a table with a key into chunks").isFalse();
        assertThat(ChunkFamily.touches(move("nexus", "plain_rows", "alter table nexus.mychunks drop column x")))
            .as("a name that only ends in chunks").isFalse();
        assertThat(ChunkFamily.touches(move("nexus", "plain_rows", null))).isFalse();
    }

    // ── the bind parameter ─────────────────────────────────────────────────────────────────────────────────

    @Test
    void theVerificationsTenantParameter_isRenderedForTheDriver() {
        String sql = "select 1 from t where tenant_id = $1 or $1 is null";
        assertThat(MoveSql.placeholders(sql)).isEqualTo(2);
        assertThat(MoveSql.bindForm(sql))
            .isEqualTo("select 1 from t where tenant_id = CAST(? AS text) or CAST(? AS text) is null");
        assertThat(MoveSql.placeholders("select 1 from t")).as("a verification with no parameter binds none").isZero();
        assertThat(MoveSql.placeholders("select $10, $1x, $1_")).as("only $1 itself").isZero();
        assertThat(MoveSql.bindForm("select 1 where j ? 'k' and tenant_id = $1"))
            .as("a ? the text already has stays a literal for the driver")
            .isEqualTo("select 1 where j ?? 'k' and tenant_id = CAST(? AS text)");
    }

    // ── on_complete's lock wait ────────────────────────────────────────────────────────────────────────────

    @Test
    void onCompletesLockTimeout_isBelowTheStatementBound_andNeverZero() {
        assertThat(VerifyingFinisher.lockTimeoutMs(60_000)).as("capped").isEqualTo(5_000);
        assertThat(VerifyingFinisher.lockTimeoutMs(4_000)).as("half").isEqualTo(2_000);
        assertThat(VerifyingFinisher.lockTimeoutMs(1)).as("0 would disable it").isEqualTo(1);
        for (int statement : new int[] {2, 3, 100, 9_999, 10_000, 10_001, 1_000_000}) {
            assertThat(VerifyingFinisher.lockTimeoutMs(statement)).isLessThan(statement).isPositive();
        }
    }
}
