// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.SQLException;
import java.sql.Statement;
import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), vectors-079: after a one-row rename a child row takes the stored collection name of the chunk it
 * references. Each test starts from {@link Rdr229ChildNameWorld#renamed}: five chunks stored under the old label, five under the new one,
 * and writers that hold only the new one. The control in each of the first three is the same statement with the trigger disabled, which
 * the chunk name key refuses: so the tests fail without vectors-079.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37ChildNameTriggerTest {

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine();
        Rdr229RenameRestartPrecondition.reach(engine).reached().require();
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private Rdr229ChildNameWorld world(String tag) throws Exception {
        return Rdr229ChildNameWorld.renamed(engine, tag, Rdr229ChildNameWorld.MODEL_384, 384);
    }

    // ---- an existing chunk and a new chunk, one table at a time ---------------------------------------------------------------

    @Test
    void manifestRowsForAnExistingChunkAndForANewChunkBothCommit_andTheExistingOneTakesTheChunksOldName() throws Exception {
        var w = world("m1");
        String t = "trg_catalog_document_chunks_name_from_chunk";
        assertThatThrownBy(() -> w.withoutTrigger("catalog_document_chunks", t, () -> w.run(w.manifestInsert("cn.m1.d0", 0, w.newName, w.ex[0]))))
            .as("control: without the trigger the chunk name key refuses a manifest row for an existing chunk under the new label")
            .hasStackTraceContaining("fk_catalog_chunks_chunk");

        w.run(w.manifestInsert("cn.m1.d0", 0, w.newName, w.ex[0]), w.manifestInsert("cn.m1.d1", 0, w.newName, w.nw[0]));

        assertThat(w.manifestNames("cn.m1.d0")).as("the row for the existing chunk carries the chunk's stored name").containsExactly(w.oldName);
        assertThat(w.manifestNames("cn.m1.d1")).as("the row for the new chunk carries the new label, as written").containsExactly(w.newName);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }

    @Test
    void chunkOrphanedAtRowsForAnExistingChunkAndForANewChunkBothCommit_andAnUpsertFindsTheExistingRow() throws Exception {
        var w = world("o1");
        String t = "trg_chunk_orphaned_at_name_from_chunk";
        assertThatThrownBy(() -> w.withoutTrigger("chunk_orphaned_at", t, () -> w.run(w.orphanedInsert(w.newName, w.ex[0]))))
            .as("control: without the trigger the key refuses it").hasStackTraceContaining("chunk_orphaned_at_chunk_fk");

        w.run(w.orphanedInsert(w.newName, w.ex[0], w.nw[0]));

        assertThat(w.orphanedNames()).as("one row stored under each chunk's name").containsExactlyInAnyOrder(w.oldName, w.newName);
        // the writers' own shape: ON CONFLICT (tenant_id, collection, chash) with the new label. The arbiter sees the corrected name,
        // so the existing row is updated and no second row appears.
        w.run(w.orphanedInsert(w.newName, w.ex[0]) + " ON CONFLICT (tenant_id, collection, chash) DO UPDATE SET orphaned_at = EXCLUDED.orphaned_at");
        assertThat(w.orphanedNames()).as("the upsert hit the row of the old name: still two rows").hasSize(2);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }

    @Test
    void topicAssignmentsForAnExistingChunkAndForANewChunkBothCommit_andTakeTheChunksName() throws Exception {
        var w = world("t1");
        String t = "trg_topic_assignments_name_from_chunk";
        assertThatThrownBy(() -> w.withoutTrigger("topic_assignments", t, () -> w.run(w.assignmentInsert(w.newName, w.ex[0]))))
            .as("control: without the trigger the key refuses it").hasStackTraceContaining("topic_assignments_chunk_fk");

        w.run(w.assignmentInsert(w.newName, w.ex[0], w.nw[0]));

        assertThat(w.assignmentNames()).containsExactlyInAnyOrder(w.oldName, w.newName);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }

    // ---- a mixed batch, and the once-per-transaction line ----------------------------------------------------------------------

    @Test
    void aMixedBatchOfExistingAndNewChunksCommitsInOneStatement_forEachTable_andIsLoggedOnce() throws Exception {
        var w = world("mix");
        // three statements, each its own transaction (autocommit): five rows each, three of them replaced, one line each, not one per row
        List<String> lines = w.run(
            w.manifestInsert("cn.mix.d0", 0, w.newName, w.ex[0], w.nw[0], w.ex[1], w.nw[1], w.ex[2]),
            w.orphanedInsert(w.newName, w.ex[0], w.nw[0], w.ex[1], w.nw[1], w.ex[2]),
            w.assignmentInsert(w.newName, w.ex[0], w.nw[0], w.ex[1], w.nw[1], w.ex[2]));

        assertThat(w.manifestNames("cn.mix.d0")).as("five rows, three of them corrected")
            .containsExactlyInAnyOrder(w.oldName, w.oldName, w.oldName, w.newName, w.newName);
        assertThat(w.orphanedNames()).hasSize(5);
        assertThat(w.assignmentNames()).hasSize(5);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
        assertThat(lines).as("three transactions, each replaced three names: one line each, not one per row").hasSize(3);
        for (String table : List.of("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments")) {
            assertThat(lines).anySatisfy(l -> assertThat(l).contains(Rdr229ChildNameWorld.LOG_PREFIX + table)
                .contains("supplied " + w.newName).contains("stored " + w.oldName).contains("application ").contains("pid "));
        }
        assertThat(lines.get(0)).as("the sample is the first row replaced, with the chunk key prefix")
            .contains("chunk " + w.hex(w.ex[0]).substring(0, 12));
    }

    @Test
    void aSecondStatementInTheSameTransactionLogsNothingMore_onAnyOfTheThreeTables_andANewTransactionLogsAgain() throws Exception {
        var w = world("tx");
        List<String> one = w.runInOneTransaction(
            w.manifestInsert("cn.tx.d0", 0, w.newName, w.ex[0], w.ex[1]),
            w.manifestInsert("cn.tx.d1", 0, w.newName, w.ex[2]),
            w.orphanedInsert(w.newName, w.ex[3]),
            w.assignmentInsert(w.newName, w.ex[4]));
        assertThat(one).as("one line for the whole transaction: four statements, three tables, five replaced rows").hasSize(1);
        assertThat(one.get(0)).contains(Rdr229ChildNameWorld.LOG_PREFIX + "catalog_document_chunks").contains("supplied " + w.newName);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();

        List<String> next = w.runInOneTransaction(w.orphanedInsert(w.newName, w.ex[0]));
        assertThat(next).as("a new transaction logs again").singleElement().asString().contains(Rdr229ChildNameWorld.LOG_PREFIX + "chunk_orphaned_at");
    }

    @Test
    void aStatementThatReplacesNothingLogsNothing_andDoesNotSetTheFlag() throws Exception {
        var w = world("quiet");
        List<String> quiet = w.run(w.manifestInsert("cn.quiet.d0", 0, w.newName, w.nw[0], w.nw[1]));
        assertThat(quiet).as("only new chunks, written under the label they carry: nothing replaced, nothing logged").isEmpty();

        List<String> tx = w.runInOneTransaction(
            w.manifestInsert("cn.quiet.d1", 0, w.newName, w.nw[2]),   // nothing replaced: the flag stays off
            w.manifestInsert("cn.quiet.d2", 0, w.newName, w.ex[0]));
        assertThat(tx).as("the first replacement of the transaction logs, although a statement before it replaced nothing").hasSize(1);
    }

    /**
     * A rolled-back savepoint: the flag is transactional, so the rollback un-sets it and the next replacement logs again. The line of the
     * rolled-back statement stays in the server log (a log line cannot be taken back), so a transaction can log twice in this case.
     */
    @Test
    void aRolledBackSavepointUnsetsTheFlag_soTheNextReplacementLogsAgain() throws Exception {
        var w = world("leak");
        // row 1 is replaced (the flag is set, the line is logged), row 2 repeats row 1's position and fails the primary key
        String failing = w.manifestInsert("cn.leak.d0", 0, w.newName, w.ex[0]) + ", ('" + w.tenant + "','cn.leak.d0',0," + "decode('"
            + w.hex(w.ex[1]) + "','hex'),'" + w.newName + "','" + w.model + "'," + w.id + ")";
        List<String> after = engine.su(ctx -> ctx.connectionResult(c -> {
            c.setAutoCommit(false);
            try (Statement s = c.createStatement()) {
                s.execute("SET client_min_messages = log");
                var sp = c.setSavepoint();
                try {
                    s.execute(failing);
                    throw new AssertionError("non-vacuity: the statement must fail");
                } catch (SQLException expected) {
                    c.rollback(sp);
                }
                s.execute(w.manifestInsert("cn.leak.d1", 0, w.newName, w.ex[2]));
                List<String> out = Rdr229ChildNameWorld.logged(s);
                s.execute(w.manifestInsert("cn.leak.d2", 0, w.newName, w.ex[3]));
                out.addAll(Rdr229ChildNameWorld.logged(s));
                c.commit();
                return out;
            }
        }));
        assertThat(after).as("the rollback un-set the flag, so the statement after it logs; the one after that is silent")
            .singleElement().asString().contains("supplied " + w.newName);
    }

    // ---- updates --------------------------------------------------------------------------------------------------------------

    @Test
    void anUpdateThatMovesAManifestRowToAnotherChunkIsCorrected_andAStampOfTheIdIsLeftAlone() throws Exception {
        var w = world("upd");
        w.run(w.manifestInsert("cn.upd.d0", 0, w.newName, w.nw[0]));   // a row stored under the new label (its chunk is new)
        assertThat(w.manifestNames("cn.upd.d0")).containsExactly(w.newName);

        // the row now points at an existing chunk (stored under the old label); the writer's UPDATE keeps the label it holds
        List<String> moved = w.run("UPDATE nexus.catalog_document_chunks SET chash = decode('" + w.hex(w.ex[0]) + "','hex') "
            + "WHERE tenant_id = '" + w.tenant + "' AND doc_id = 'cn.upd.d0' AND position = 0");
        assertThat(w.manifestNames("cn.upd.d0")).as("the UPDATE moved the key, so the name follows the chunk").containsExactly(w.oldName);
        assertThat(moved).singleElement().asString().contains("supplied " + w.newName).contains("stored " + w.oldName);

        List<String> stamp = w.run("UPDATE nexus.catalog_document_chunks SET collection_id = collection_id WHERE tenant_id = '" + w.tenant + "'");
        assertThat(stamp).as("an UPDATE that leaves the key alone (the stamp's shape) costs no lookup and logs nothing").isEmpty();
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }
}
