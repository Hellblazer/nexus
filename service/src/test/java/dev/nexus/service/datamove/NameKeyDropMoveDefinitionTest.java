// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27): the name-key drop move's definition and the readiness arithmetic, without a database. The move runs
 * for real in {@code Rdr229P37RenameWindowTest} and {@code Rdr229P37OneRowRenameTest}.
 */
class NameKeyDropMoveDefinitionTest {

    private static DataMove move() {
        return new DataMove(NameKeyDropMove.LABEL, NameKeyDropMove.TEMPLATE, NameKeyDropMove.DRIVING_SCHEMA, NameKeyDropMove.DRIVING_TABLE,
            NameKeyDropMove.CHUNK_SIZE, NameKeyDropMove.SLEEP_MS, NameKeyDropMove.STATEMENT_TIMEOUT_MS, NameKeyDropMove.VERIFICATION,
            NameKeyDropMove.onComplete(), null, null, null, 0, null, null, DataMove.DEFAULT_VERIFY_STATEMENT_TIMEOUT_MS);
    }

    @Test
    void theMoveIsOverTheRegistryAndIsClassedChunkFamilyByItsOnComplete() {
        DataMove m = move();
        assertThat(m.drivingTable()).as("the small registry table, so the move is short").isEqualTo("catalog_collections");
        assertThat(ChunkFamily.touches(m))
            .as("on_complete names nexus.chunks and topic_assignments, so VerifyingFinisher ends the index builders first (RDR-246)")
            .isTrue();
        assertThat(ChunkFamily.touches(new DataMove(m.label(), m.template(), m.drivingSchema(), m.drivingTable(), m.chunkSize(),
            m.sleepMs(), m.statementTimeoutMs(), m.verification(), null, null, null, null, 0, null, null,
            m.verifyStatementTimeoutMs())))
            .as("non-vacuity: with no on_complete the driving table alone is not chunk-family").isFalse();
    }

    @Test
    void onCompleteDropsEveryNameKeyTheTwinsListBesideTheChunksKey_andTheQueuesUnderItsGuard() {
        String sql = NameKeyDropMove.onComplete();
        Set<String> listed = new TreeSet<>();
        for (FkTwins.Row r : FkTwins.load()) {
            if (!r.table().equals(NameKeyDropMove.QUEUE_KEY.table())) {
                listed.add(r.nameKey());
            }
        }
        listed.add("chunks_collection_fk");
        Set<String> dropped = new TreeSet<>();
        for (NameKeyDropMove.NameKey k : NameKeyDropMove.NAME_KEYS) {
            dropped.add(k.constraint());
            assertThat(sql).contains("DROP CONSTRAINT IF EXISTS " + k.constraint() + ";");
        }
        assertThat(dropped).as("the seven keys of RDR item 6, the P3.3a list's name keys and chunks_collection_fk").hasSize(7)
            .isEqualTo(listed);
        int guard = sql.indexOf("to_regclass('nexus." + NameKeyDropMove.QUEUE_KEY.table() + "') IS NOT NULL");
        assertThat(guard).as("the queue's key is dropped only where the table exists").isPositive();
        assertThat(sql.indexOf("DROP CONSTRAINT IF EXISTS " + NameKeyDropMove.QUEUE_KEY.constraint())).isGreaterThan(guard);
        assertThat(sql.indexOf("nexus." + NameKeyDropMove.QUEUE_KEY.table() + " DROP")).isGreaterThan(guard);
    }

    @Test
    void templateAndVerificationAreTheNoOpRestampOverNullIds() {
        assertThat(NameKeyDropMove.TEMPLATE).contains("<driving_table>").contains("<chunking_clause>")
            .contains("t.collection_id IS NULL");
        assertThat(NameKeyDropMove.VERIFICATION).contains("t.collection_id IS NULL").contains("$1");
    }

    @Test
    void registerIfAbsentRegistersNothingBeforeGate3OrBeforeTheFirstSample() {
        var ctx = DSL.using(SQLDialect.POSTGRES);   // never reached: the guard returns first
        assertThat(NameKeyDropMove.registerIfAbsent(null, ctx)).isFalse();
        assertThat(NameKeyDropMove.registerIfAbsent(result(false, RenameState(false, 0, List.of())), ctx)).isFalse();
    }

    // ---- the readiness arithmetic ----------------------------------------------------------------------------------------

    private static CompletionCheck.RenameState RenameState(boolean dropped, int listed, List<String> missing) {
        return new CompletionCheck.RenameState(dropped, listed, missing);
    }

    private static CompletionCheck.Result result(boolean gate3, CompletionCheck.RenameState rename) {
        return new CompletionCheck.Result(true, true, gate3, List.of(), List.of(), true, 0L, List.of(), List.of(), "",
            CompletionCheck.Dangling.UNREAD, rename);
    }

    @Test
    void renameMissing_isNoneBeforeGate3_thenTheMove_thenTheRestart_thenNone() {
        assertThat(result(false, RenameState(false, 3, List.of("a"))).renameMissing())
            .as("before gate 3 nothing is awaited: the rename is the name path").isEqualTo(RenameReadiness.Missing.NONE);
        assertThat(result(true, RenameState(false, 3, List.of())).renameMissing())
            .as("a label with no row, or no completed_at, is the move").isEqualTo(RenameReadiness.Missing.NAME_KEY_DROP_MOVE);
        assertThat(result(true, RenameState(true, 3, List.of("a"))).renameMissing()).isEqualTo(RenameReadiness.Missing.P35B_RESTART);
        assertThat(result(true, RenameState(true, 0, List.of())).renameMissing())
            .as("an empty or missing list is refused, not read as 'all applied'").isEqualTo(RenameReadiness.Missing.P35B_RESTART);
        assertThat(result(true, RenameState(true, 3, List.of())).renameMissing()).isEqualTo(RenameReadiness.Missing.NONE);
    }

    @Test
    void restartNeeded_onlyWhenTheStampsAreDoneAndAListedChangesetIsUnapplied() {
        assertThat(result(false, RenameState(false, 2, List.of("a"))).restartNeeded()).isTrue();
        assertThat(result(false, RenameState(false, 2, List.of())).restartNeeded()).as("all applied: no restart").isFalse();
        assertThat(result(false, RenameState(false, 0, List.of())).restartNeeded())
            .as("no list: a restart cannot fix a missing list, so no entry").isFalse();
        var gate1Waiting = new CompletionCheck.Result(false, false, false, List.of(), List.of(), true, 0L, List.of(), List.of(), "",
            CompletionCheck.Dangling.UNREAD, RenameState(false, 2, List.of("a")));
        assertThat(gate1Waiting.restartNeeded()).as("the stamp moves are not complete yet").isFalse();
    }

    @Test
    void theListParsesOneIdPerLineWithCommentsAndBlanksSkipped() throws Exception {
        String text = "# P3.5b\n\nvectors-075-1\n  vectors-075-2  \n# tail\n";
        assertThat(P35bChangesets.parse(new ByteArrayInputStream(text.getBytes(StandardCharsets.UTF_8))))
            .containsExactlyInAnyOrder("vectors-075-1", "vectors-075-2");
    }
}
