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

    // ---- a mixed batch, and the once-per-statement line ------------------------------------------------------------------------

    @Test
    void aMixedBatchOfExistingAndNewChunksCommitsInOneStatement_forEachTable_andIsLoggedOncePerStatement() throws Exception {
        var w = world("mix");
        List<String> lines = w.run(
            w.manifestInsert("cn.mix.d0", 0, w.newName, w.ex[0], w.nw[0], w.ex[1], w.nw[1], w.ex[2]),
            w.orphanedInsert(w.newName, w.ex[0], w.nw[0], w.ex[1], w.nw[1], w.ex[2]),
            w.assignmentInsert(w.newName, w.ex[0], w.nw[0], w.ex[1], w.nw[1], w.ex[2]));

        assertThat(w.manifestNames("cn.mix.d0")).as("five rows, three of them corrected")
            .containsExactlyInAnyOrder(w.oldName, w.oldName, w.oldName, w.newName, w.newName);
        assertThat(w.orphanedNames()).hasSize(5);
        assertThat(w.assignmentNames()).hasSize(5);
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
        assertThat(lines).as("three statements replaced three names each: one line each, not one per row").hasSize(3);
        for (String table : List.of("catalog_document_chunks", "chunk_orphaned_at", "topic_assignments")) {
            assertThat(lines).anySatisfy(l -> assertThat(l).contains(Rdr229ChildNameWorld.LOG_PREFIX + table)
                .contains("on 3 row(s) of one statement").contains(" -> " + w.oldName));
        }
        assertThat(lines.get(0)).as("the sample is the first row replaced: supplied -> stored").contains("first: " + w.newName + " -> " + w.oldName);
    }

    @Test
    void aStatementThatReplacesNothingLogsNothing_andTheNextCountStartsAtOne() throws Exception {
        var w = world("quiet");
        List<String> quiet = w.run(w.manifestInsert("cn.quiet.d0", 0, w.newName, w.nw[0], w.nw[1]));
        assertThat(quiet).as("only new chunks, written under the label they carry: nothing replaced, nothing logged").isEmpty();

        List<String> first = w.run(w.manifestInsert("cn.quiet.d1", 0, w.newName, w.ex[0], w.ex[1]));
        List<String> second = w.run(w.manifestInsert("cn.quiet.d2", 0, w.newName, w.ex[2]));
        assertThat(first).singleElement().asString().contains("on 2 row(s)");
        assertThat(second).as("the count is per statement, not cumulative").singleElement().asString().contains("on 1 row(s)");
    }

    @Test
    void aFailedStatementsCountDoesNotLeakIntoTheNextStatementOfTheTransaction() throws Exception {
        var w = world("leak");
        // row 1 is replaced (the count is set), row 2 repeats row 1's position and fails the primary key before the statement trigger can run
        String failing = w.manifestInsert("cn.leak.d0", 0, w.newName, w.ex[0]) + ", ('" + w.tenant + "','cn.leak.d0',0," + "decode('"
            + w.hex(w.ex[1]) + "','hex'),'" + w.newName + "','" + w.model + "'," + w.id + ")";
        List<String> lines = engine.su(ctx -> ctx.connectionResult(c -> {
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
                c.commit();
                return out;
            }
        }));
        assertThat(lines).as("the rolled-back statement's replaced row is not counted: this statement replaced one").singleElement()
            .asString().contains("on 1 row(s)");
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
        assertThat(moved).singleElement().asString().contains("on 1 row(s)");

        List<String> stamp = w.run("UPDATE nexus.catalog_document_chunks SET collection_id = collection_id WHERE tenant_id = '" + w.tenant + "'");
        assertThat(stamp).as("an UPDATE that leaves the key alone (the stamp's shape) costs no lookup and logs nothing").isEmpty();
        assertThat(w.childrenNotOnTheirChunksName()).isZero();
    }
}
