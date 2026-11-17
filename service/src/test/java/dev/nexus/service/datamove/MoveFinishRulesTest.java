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

    @Test
    void theChunkFamilyAlsoNamesThePartitionHelpersAndTheBuilder() {
        assertThat(ChunkFamily.touches(move("dm_test", "plain_rows", "select nexus.partition_ensure_leaf('m1', 'acme')")))
            .as("a partition helper").isTrue();
        assertThat(ChunkFamily.touches(move("dm_test", "plain_rows", "drop index pci_acme_1"))).as("the builder").isTrue();
        assertThat(ChunkFamily.touches(move("dm_test", "plain_rows", "alter table nexus.partitioned_things add column x int")))
            .as("partitioned_ is not partition_").isFalse();
    }

    // ── the bind parameter ─────────────────────────────────────────────────────────────────────────────

    @Test
    void theVerificationsTenantParameter_isRenderedForTheDriver() {
        String sql = "select 1 from t where tenant_id = $1 or $1 is null";
        assertThat(MoveSql.refusal(sql)).isEmpty();
        assertThat(MoveSql.placeholders(sql)).isEqualTo(2);
        assertThat(MoveSql.bindForm(sql))
            .isEqualTo("select 1 from t where tenant_id = CAST(? AS text) or CAST(? AS text) is null");
        assertThat(MoveSql.placeholders("select 1 from t")).as("a verification with no parameter binds none").isZero();
        assertThat(MoveSql.refusal("select 1 from t where a = $1)")).as("a bare $1 before a paren").isEmpty();
        assertThat(MoveSql.refusal("select 1 from t where a in ($1,$1)")).as("a bare $1 before a comma").isEmpty();
        assertThat(MoveSql.refusal("select $a$ x $a$ from t where a = $1")).as("a dollar quote with a letter tag").isEmpty();
    }

    @Test
    void aTextWithAQuestionMark_orADollarDigitThatIsNotAStandaloneDollarOne_isRefusedNotRepaired() {
        assertThat(MoveSql.refusal("select 1 where j ? 'k'")).hasValueSatisfying(r -> assertThat(r).contains("'?'"));
        assertThat(MoveSql.refusal("select 1 where p like '%?%'")).as("a ? inside a literal").isPresent();
        for (String text : new String[] {
            "select 1 where a = $2", "select 1 where a = $10", "select 1 where a = $1x", "select 1 where a = my$1",
            "select 1 where a = $$1 x$$", "select 1 where a = $1$", "select 1 where a = _$1", "select 1 where a = $11"}) {
            assertThat(MoveSql.refusal(text)).as(text).hasValueSatisfying(r -> assertThat(r).contains("not a standalone $1"));
        }
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
