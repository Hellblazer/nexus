// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;

import java.sql.Connection;
import java.util.function.Function;

/**
 * Shared fixture of the P3.5c "rest" readers' tests: rows written under a registered collection by superuser jOOQ, a registry
 * row renamed in place (FK triggers off, the state P3.7's one-row rename leaves), a registry row superseded in place (a tombstone
 * that keeps its data) and a tombstone row beside a live one. Every {@code insert*} takes the collection id explicitly or, with
 * {@code null}, stamps the registry's id of the name.
 */
abstract class Rdr229P35cRestFixture extends AtomicWriteTestBase {

    static final String MODEL = "minilm-l6-v2-384";

    <T> T su(Function<DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    String register(String tenant, String type, String tag) throws Exception {
        String c = type + "__p35cx" + tag + seq.incrementAndGet() + "__" + MODEL + "__v1";
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, c);
            return null;
        });
        return c;
    }

    /** Renames the registry row of {@code old} to {@code neu} in place; the children keep {@code old}. */
    String renameRegistryRow(String tenant, String old, String neu) throws Exception {
        su(ctx -> {
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, neu)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(old)).execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
            return null;
        });
        return neu;
    }

    String renameRegistryRow(String tenant, String old) throws Exception {
        return renameRegistryRow(tenant, old, old.replaceFirst("p35cx", "p35cn"));
    }

    /** Deletes the registry row of {@code name} with FK triggers off (the client deletes it before its ledger cleanup). */
    void deleteRegistryRow(String tenant, String name) throws Exception {
        su(ctx -> {
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                    .and(CATALOG_COLLECTIONS.NAME.eq(name)).execute();
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
            return null;
        });
    }

    Long registryId(String tenant, String name) throws Exception {
        return su(ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID.desc()).limit(1).fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    /** Supersedes the registry row of {@code name} in place (it keeps its data and its id) under a fresh live successor; its id. */
    Long supersede(String tenant, String name) throws Exception {
        String successor = register(tenant, "code", "sc");
        Long successorId = registryId(tenant, successor);
        su(ctx -> ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successorId)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute());
        return registryId(tenant, name);
    }

    byte[] nextChash() {
        int n = seq.incrementAndGet();
        byte[] b = new byte[32];
        b[0] = (byte) (n >> 8);
        b[1] = (byte) n;
        b[31] = 7;
        return b;
    }

    void insertChunk(String tenant, String collection, Long id, byte[] chash, String metadataJson) throws Exception {
        float[] v = new float[384];
        v[0] = 1f;
        su(ctx -> ctx.insertInto(CHUNKS)
            .set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, collection).set(CHUNKS.CHASH, chash)
            .set(CHUNKS.EMBEDDING_MODEL, MODEL).set(CHUNKS.CHUNK_TEXT, "p35c rest")
            .set(CHUNKS.COLLECTION_ID, id != null ? DSL.val(id) : CollectionIds.idOf(tenant, collection))
            .set(CHUNKS.METADATA, JSONB.valueOf(metadataJson))
            .set(CHUNKS.EMBEDDING_384, Vector.of(v)).execute());
    }

    void insertChunk(String tenant, String collection, byte[] chash, String metadataJson) throws Exception {
        insertChunk(tenant, collection, null, chash, metadataJson);
    }

    void insertDocument(String tenant, String tumbler, String collection, Long id, String contentType, String filePath,
                        String sourceUri, String indexState, String indexContentHash) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "doc " + tumbler).set(CATALOG_DOCUMENTS.CONTENT_TYPE, contentType)
            .set(CATALOG_DOCUMENTS.FILE_PATH, filePath).set(CATALOG_DOCUMENTS.SOURCE_URI, sourceUri)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, id != null ? DSL.val(id) : CollectionIds.idOf(tenant, collection))
            .set(CATALOG_DOCUMENTS.INDEX_STATE, indexState.isEmpty() ? null : indexState)
            .set(CATALOG_DOCUMENTS.INDEX_CONTENT_HASH, indexContentHash)
            .set(CATALOG_DOCUMENTS.METADATA, JSONB.valueOf("{}"))
            .execute());
    }

    void insertDocument(String tenant, String tumbler, String collection, String contentType, String filePath,
                        String sourceUri, String indexState, String indexContentHash) throws Exception {
        insertDocument(tenant, tumbler, collection, null, contentType, filePath, sourceUri, indexState, indexContentHash);
    }

    void insertManifest(String tenant, String tumbler, int position, String collection, Long id, byte[] chash) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, tumbler)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position).set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, MODEL)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, id != null ? DSL.val(id) : CollectionIds.idOf(tenant, collection))
            .execute());
    }

    void insertManifest(String tenant, String tumbler, int position, String collection, byte[] chash) throws Exception {
        insertManifest(tenant, tumbler, position, collection, null, chash);
    }
}
