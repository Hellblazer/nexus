// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Test Plan item (nexus-3wh8d.19): the orphaned-at family against the partitioned {@code nexus.chunks}.
 * {@code chunk_orphaned_at} is keyed by (tenant, collection, chash) but its foreign key to the chunk is the
 * four-column {@code (tenant_id, collection, chash, embedding_model)}. The stamp triggers must therefore carry the
 * CHUNK's model, join the manifest row to the chunk on all four columns, and the foreign key must refuse a record
 * naming another model's chunk and cascade per model.
 *
 * <p>The fixture holds the SAME chash under two collections of two different models of one width (voyage-code-3 and
 * voyage-context-3, both 1024-d), the only shape where a model column does work the collection column does not
 * already do. Everything runs as the schema owner: the triggers are SECURITY INVOKER and the tenant scoping is
 * pinned by {@code ChunkIsReapableIntegrationTest}; this class pins the model.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OrphanedAtFkPartitionedParentIntegrationTest {

    private static final String CODE_3 = "voyage-code-3";
    private static final String CONTEXT_3 = "voyage-context-3";
    private static final String CODE_COL = "code__oafk-c__voyage-code-3__v1";
    private static final String CTX_COL = "docs__oafk-d__voyage-context-3__v1";

    private PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    private void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private static byte[] bytes(String hex) {
        return Chash.fromHex(hex).toBytes();
    }

    private static float[] vec1024() {
        float[] v = new float[1024];
        v[0] = 1f;
        return v;
    }

    /**
     * One tenant holding chash {@code shared} under both collections (each under its own model) plus a second
     * chunk {@code other} under the code collection, all aged past the one-hour stamp guard, with one manifest row
     * per collection naming {@code shared} and a third naming {@code other}.
     */
    private record Fixture(String tenant, String shared, String other) {}

    private Fixture seed(String tenant) throws Exception {
        String shared = Chash.ofText(tenant + "/shared").toHex();
        String other = Chash.ofText(tenant + "/other").toHex();
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, CODE_COL, CODE_3);
            PgContainerHelper.insertCollection(ctx, tenant, CTX_COL, CONTEXT_3);
            PgContainerHelper.insertChunks(ctx, tenant, CODE_COL, List.of(shared, other), List.of("shared text", "other text"),
                List.of(vec1024(), vec1024()), List.of(Map.of(), Map.of()));
            PgContainerHelper.insertChunks(ctx, tenant, CTX_COL, List.of(shared), List.of("shared text"),
                List.of(vec1024()), List.of(Map.of()));
            OffsetDateTime old = OffsetDateTime.now().minusHours(3);
            ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, old).set(CHUNKS.LAST_WRITTEN_AT, old)
               .where(CHUNKS.TENANT_ID.eq(tenant)).execute();
            for (String[] d : new String[][] {{"d-code", CODE_COL}, {"d-ctx", CTX_COL}}) {
                ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                        CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                    .values(tenant, d[0], "doc " + d[0], d[1]).execute();
            }
            manifest(ctx, tenant, "d-code", CODE_COL, shared, 0);
            manifest(ctx, tenant, "d-ctx", CTX_COL, shared, 0);
        });
        return new Fixture(tenant, shared, other);
    }

    private static void manifest(DSLContext ctx, String tenant, String doc, String collection, String hex, int position) {
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
            .values(tenant, doc, position, bytes(hex), collection, PgContainerHelper.collectionModel(ctx, tenant, collection))
            .execute();
    }

    /** "collection/model" of every orphaning record of the tenant, sorted. */
    private TreeSet<String> records(String tenant) throws Exception {
        TreeSet<String> out = new TreeSet<>();
        su(ctx -> ctx.select(CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.EMBEDDING_MODEL).from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant))
            .fetch().forEach(r -> out.add(r.value1() + "/" + r.value2())));
        return out;
    }

    private static String sqlState(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof DataAccessException d && d.sqlState() != null) return d.sqlState();
        }
        return null;
    }

    @Test
    void droppingOneModelsManifestRow_stampsOnlyThatModelsChunk_carryingThatModel() throws Exception {
        Fixture f = seed("oafk-delete");
        assertThat(records(f.tenant())).as("control: nothing is orphaned while both collections hold the chash").isEmpty();

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(f.tenant()).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("d-code"))).execute());
        assertThat(records(f.tenant()))
            .as("the code collection's copy is stamped with the code model; the context collection's copy is untouched")
            .containsExactly(CODE_COL + "/" + CODE_3);

        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(f.tenant()).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("d-ctx"))).execute());
        assertThat(records(f.tenant()))
            .containsExactly(CODE_COL + "/" + CODE_3, CTX_COL + "/" + CONTEXT_3);
    }

    @Test
    void replacingAManifestRowsChash_stampsTheOldChashWithTheChunksModel() throws Exception {
        Fixture f = seed("oafk-update");
        su(ctx -> ctx.update(CATALOG_DOCUMENT_CHUNKS).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(f.other()))
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(f.tenant()).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("d-code"))).execute());
        assertThat(records(f.tenant()))
            .as("the displaced chash of the code collection is stamped with the code model, and only it")
            .containsExactly(CODE_COL + "/" + CODE_3);
    }

    @Test
    void theRecordsForeignKeyRefusesAnotherModelsChunk_andCascadesPerModel() throws Exception {
        Fixture f = seed("oafk-fk");
        // (CODE_COL, shared) exists only under the code model: a record naming it under the context model has no chunk.
        assertThatThrownBy(() -> su(ctx -> ctx.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID,
                CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH, CHUNK_ORPHANED_AT.ORPHANED_AT,
                CHUNK_ORPHANED_AT.EMBEDDING_MODEL)
            .values(f.tenant(), CODE_COL, bytes(f.shared()), OffsetDateTime.now(), CONTEXT_3).execute()))
            .satisfies(t -> assertThat(sqlState(t)).as("foreign_key_violation").isEqualTo("23503"));
        assertThat(records(f.tenant())).isEmpty();

        // Stamp both copies, then remove one chunk: only that model's record goes with it.
        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(f.tenant())).execute());
        assertThat(records(f.tenant())).containsExactly(CODE_COL + "/" + CODE_3, CTX_COL + "/" + CONTEXT_3);
        su(ctx -> ctx.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(f.tenant()).and(CHUNKS.COLLECTION.eq(CODE_COL))
            .and(CHUNKS.CHASH.eq(bytes(f.shared())))).execute());
        assertThat(records(f.tenant())).as("ON DELETE CASCADE removed the deleted chunk's record and not its twin's")
            .containsExactly(CTX_COL + "/" + CONTEXT_3);
    }
}
