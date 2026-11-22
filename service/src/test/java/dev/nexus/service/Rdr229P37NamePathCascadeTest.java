// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), critic r2 item 2: a pre-gate-3 NAME-PATH rename with vectors-079 installed. The rename retires the old
 * registry row, gives the collection a new id and renames the chunks first; the chunk foreign keys' ON UPDATE CASCADE then renames each
 * manifest, orphaned-at and topic-assignment row by name while it still carries the old id. That cascade is not a writer: step 2 of the
 * trigger (a by-id scan of the tenant's leaf, no id key on chunks in this tree) would run once per cascaded row, N x N. The trigger skips
 * it for a child whose collection id belongs to a tombstone. The rename must complete, leave every child on its chunk's name, and cost
 * about what it costs with the triggers disabled.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P37NamePathCascadeTest {

    private static final int N = 4000;
    private static final List<String> TRIGGERS = List.of(
        "catalog_document_chunks:trg_catalog_document_chunks_name_from_chunk", "chunk_orphaned_at:trg_chunk_orphaned_at_name_from_chunk",
        "topic_assignments:trg_topic_assignments_name_from_chunk");

    private Rdr229RenameEngine engine;

    @BeforeAll
    void boot() throws Exception {
        engine = new Rdr229RenameEngine().gate3(false);   // never opens: the name path throughout
        engine.boot();
        Rdr229RenameRestartPrecondition.awaitStampMovesComplete(engine);
    }

    @AfterAll
    void stop() {
        if (engine != null) {
            engine.close();
        }
    }

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    /** One chunk, then N more copied from it with other chashes; a manifest row per chunk, an orphaned-at and a topic assignment for each of the first N/2. */
    private void seed(String tenant, String name) {
        engine.su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, name);
            long id = Rdr229RenameChecks.registryId(ctx, tenant, name);
            String model = PgContainerHelper.collectionModel(ctx, tenant, name);
            Rdr229ChildNameWorld.chunk(ctx, tenant, name, id, model, 384, "seed");
            ctx.execute("INSERT INTO nexus.chunks (tenant_id, collection, chash, embedding_model, chunk_text, embedding_384, metadata, "
                + "last_written_at, collection_id) SELECT c.tenant_id, c.collection, sha256(c.chash || g::text::bytea), c.embedding_model, "
                + "c.chunk_text, c.embedding_384, c.metadata, c.last_written_at, c.collection_id "
                + "FROM nexus.chunks c, generate_series(1, ?) g WHERE c.tenant_id = ? AND c.collection = ?", N, tenant, name);
            PgContainerHelper.insertCatalogDocument(ctx, tenant, "casc.doc." + name);
            ctx.execute("INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection, embedding_model, collection_id) "
                + "SELECT tenant_id, ?, (row_number() OVER (ORDER BY chash)) - 1, chash, collection, embedding_model, collection_id "
                + "FROM nexus.chunks WHERE tenant_id = ? AND collection = ?", "casc.doc." + name, tenant, name);
            ctx.execute("INSERT INTO nexus.chunk_orphaned_at (tenant_id, collection, chash, orphaned_at, embedding_model, collection_id) "
                + "SELECT tenant_id, collection, chash, now(), embedding_model, collection_id FROM nexus.chunks "
                + "WHERE tenant_id = ? AND collection = ? ORDER BY chash LIMIT ?", tenant, name, N / 2);
            long topic = ctx.fetchOne("INSERT INTO nexus.topics (tenant_id, label, collection, collection_id, created_at) "
                + "VALUES (?, 't', ?, ?, now()) RETURNING id", tenant, name, id).get(0, Long.class);
            ctx.execute("INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by, source_collection, embedding_model, "
                + "source_collection_id) SELECT tenant_id, chash, ?, 'seed', collection, embedding_model, collection_id FROM nexus.chunks "
                + "WHERE tenant_id = ? AND collection = ? ORDER BY chash LIMIT ?", topic, tenant, name, N / 2);
            return null;
        });
    }

    private long count(String sql, String tenant, String name) {
        return engine.su(ctx -> ctx.fetchOne(sql, tenant, name).get(0, Long.class));
    }

    private long[] childCounts(String tenant, String name) {
        return new long[] {
            count("SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id = ? AND collection = ?", tenant, name),
            count("SELECT count(*) FROM nexus.chunk_orphaned_at WHERE tenant_id = ? AND collection = ?", tenant, name),
            count("SELECT count(*) FROM nexus.topic_assignments WHERE tenant_id = ? AND source_collection = ?", tenant, name)};
    }

    private long renameMillis(String tenant, String token, String oldName, String newName) throws Exception {
        long[] before = childCounts(tenant, oldName);
        assertThat(before).as("non-vacuity: children to cascade").containsExactly(N + 1L, N / 2L, N / 2L);
        long started = System.nanoTime();
        var r = engine.rename(token, oldName, newName);
        long ms = (System.nanoTime() - started) / 1_000_000;
        assertThat(r.status()).as("the name path before gate 3: " + r).isEqualTo(200);
        assertThat(childCounts(tenant, oldName)).as("no child is left under the old name").containsExactly(0L, 0L, 0L);
        assertThat(childCounts(tenant, newName)).as("every child follows its chunk to the new name").containsExactly(before);
        assertThat(Rdr229ChildNameWorld.childrenNotOnTheirChunksName(engine, tenant)).as("the children are consistent with the chunks").isZero();
        return ms;
    }

    @Test
    void aNamePathRenameCascadesEveryChildWithTheTriggersInstalled_atAboutTheCostOfNoTriggers() throws Exception {
        String tenant = "p37-casc";
        String token = engine.newTenant(tenant);
        String disabledOld = coll("p37cascb", 1);
        String enabledOld = coll("p37casca", 1);
        seed(tenant, disabledOld);
        seed(tenant, enabledOld);

        // control: the same rename with the three triggers disabled
        for (String t : TRIGGERS) {
            engine.su(ctx -> ctx.execute("ALTER TABLE nexus." + t.split(":")[0] + " DISABLE TRIGGER " + t.split(":")[1]));
        }
        long withoutMs;
        try {
            withoutMs = renameMillis(tenant, token, disabledOld, coll("p37cascb", 2));
        } finally {
            for (String t : TRIGGERS) {
                engine.su(ctx -> ctx.execute("ALTER TABLE nexus." + t.split(":")[0] + " ENABLE TRIGGER " + t.split(":")[1]));
            }
        }
        long withMs = renameMillis(tenant, token, enabledOld, coll("p37casca", 2));

        System.out.println("TIMING name-path rename of " + (N + 1) + " chunks, " + (N + 1) + " manifest, " + N / 2 + " orphaned, " + N / 2
            + " assignment rows: triggers disabled " + withoutMs + " ms, installed " + withMs + " ms");
        assertThat(withMs).as("the cascade costs about what it costs without the triggers (disabled " + withoutMs + " ms): no by-id scan per row")
            .isLessThanOrEqualTo(withoutMs * 3 + 3000);
    }
}
