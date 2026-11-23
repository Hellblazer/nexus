// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the re-home does not depend on the chunk foreign keys' ON UPDATE cascade. The mover files the source's chunks
 * under the target and, with {@code fk_catalog_chunks_chunk} and {@code topic_assignments_chunk_fk} (and {@code chunk_orphaned_at_chunk_fk}) present, the cascade carried the manifest rows and the
 * topic assignments along; the name key and those foreign keys go at P3.7, and a row they did not carry kept the source's label, so the re-home reported
 * the manifest as never done. This class drops both foreign keys on its own database and runs a re-home with a stored chunk, a manifest row and an
 * assignment: each lands under the TARGET's label with the target's id, and nothing is left at the source.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRehomeWithoutChunkFksTest {

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fx.buildIdTwins();
        fx.gate3.set(true);   // the id-keyed re-home, as from gate 3
        fx.asSuperuser(ctx -> {
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("fk_catalog_chunks_chunk").execute();
            ctx.alterTable(TOPIC_ASSIGNMENTS).dropConstraintIfExists("topic_assignments_chunk_fk").execute();
            ctx.alterTable(CHUNK_ORPHANED_AT).dropConstraintIfExists("chunk_orphaned_at_chunk_fk").execute();
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    @Test
    void aRehomeCarriesTheManifestRowsAndTheAssignments_withNoChunkForeignKeyToCascadeThem() {
        String source = label("rhfk-src-" + (n++));
        String target = label("rhfk-tgt-" + (n++));
        fx.register(source, target);
        long sid = fx.idOf(source);
        long tid = fx.idOf(target);
        String hex = Chash.ofText("rhfk-" + source).toHex();
        String doc = "rhfk-doc-" + n;
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, source, List.of(hex), List.of("rehome text"), List.of(new float[384]),
                List.of(Map.<String, Object>of()));
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, source);
            CollectionIdSeeds.manifestRows(ctx, fx.tenant, doc, source, 0, hex);
            return null;
        });
        long topic = fx.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(source) + ",\"label\":\"t\",\"doc_count\":1}").json().path("id").asLong(-1);
        Resp assigned = fx.post("/v1/taxonomy/assignments/assign", "{\"doc_id\":\"" + hex + "\",\"topic_id\":" + topic
            + ",\"assigned_by\":\"manual\",\"source_collection\":" + json(source) + "}");
        assertThat(assigned.status()).as("setup: " + assigned.body()).isEqualTo(200);
        java.time.OffsetDateTime orphanedAt = java.time.OffsetDateTime.now();
        fx.asSuperuser(ctx -> ctx.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.COLLECTION_ID,
                CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.ORPHANED_AT)
            .values(fx.tenant, source, sid, Chash.fromHex(hex).toBytes(), orphanedAt)
            .onConflict(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH)
            .doUpdate().set(CHUNK_ORPHANED_AT.ORPHANED_AT, orphanedAt).execute());

        Resp r = fx.post("/v1/catalog/collections/rehome", "{\"source\":" + json(source) + ",\"target\":" + json(target) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        org.jooq.Record2<String, Long> chunkRow = fx.asSuperuser(ctx -> ctx.select(CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID).from(CHUNKS)
            .where(CHUNKS.CHASH.eq(chash)).fetchOne());
        assertThat(chunkRow.value1()).as("the chunk is the target's").isEqualTo(target);
        assertThat(chunkRow.value2()).isEqualTo(tid);
        org.jooq.Record2<String, Long> manifestRow = fx.asSuperuser(ctx -> ctx.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID)
            .from(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)).fetchOne());
        assertThat(manifestRow.value1()).as("the manifest row was carried by the mover itself, under the target's label").isEqualTo(target);
        assertThat(manifestRow.value2()).as("and id").isEqualTo(tid);
        org.jooq.Record2<String, Long> assignmentRow = fx.asSuperuser(ctx -> ctx.select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID)
            .from(TOPIC_ASSIGNMENTS).where(TOPIC_ASSIGNMENTS.DOC_ID.eq(chash)).fetchOne());
        assertThat(assignmentRow.value1()).as("and so was the assignment").isEqualTo(target);
        assertThat(assignmentRow.value2()).isEqualTo(tid);
        org.jooq.Result<org.jooq.Record2<String, Long>> orphanMarks = fx.asSuperuser(ctx -> ctx.select(CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.COLLECTION_ID)
            .from(CHUNK_ORPHANED_AT).where(CHUNK_ORPHANED_AT.TENANT_ID.eq(fx.tenant)).and(CHUNK_ORPHANED_AT.CHASH.eq(chash)).fetch());
        int orphanMarkCount = orphanMarks.size();
        assertThat(orphanMarkCount).as("the orphan mark moved with its chunk, once").isEqualTo(1);
        assertThat(orphanMarks.get(0).value1()).as("under the target's label").isEqualTo(target);
        assertThat(orphanMarks.get(0).value2()).as("and id").isEqualTo(tid);
        int left = fx.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID.eq(sid)));
        assertThat(left).as("nothing of the source is left in the manifest").isEqualTo(0);
    }
}
