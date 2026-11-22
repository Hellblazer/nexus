// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;

import java.util.LinkedHashMap;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * RDR-229 P3.1 stage 2a (nexus-p2m1x.17): the test seeds that name a collection also set its {@code collection_id}.
 *
 * <p>Since vectors-068 a row that names a collection must carry the registry's id beside the name (a NOT VALID CHECK,
 * {@code (NULLIF(name, '') IS NULL) = (id IS NULL)}), and {@code nexus.chunks} must have one at all. A seed
 * that inserts the name alone fails with 23514, so the shared seeds ({@link PgContainerHelper#insertChunk384} and its
 * siblings, {@link PgContainerHelper#insertChunks}, {@link PgContainerHelper#ownChunks}) route through here. The id
 * is the registry's, read through {@link CollectionIds} on the seed's own connection, so the collection must already
 * be registered ({@link PgContainerHelper#insertCollection}), as the chunk seeds already required for the model.
 *
 * <p>Layouts: the id columns exist only from vectors-068. Containers migrated part way (the pre-walk fixtures, the
 * upgrade rehearsals) seed an older layout, so every seed asks {@link #hasIdColumns} first and writes the older row
 * unchanged when it is false.
 *
 * <p>Row shape: the seeds build a column-to-value map ({@link Registered#chunkKey}) and add what they carry, because the set
 * of columns depends on the layout and a positional {@code values(...)} cannot vary.
 */
public final class CollectionIdSeeds {

    private CollectionIdSeeds() {}

    /**
     * Registers {@code collections} for {@code tenant} on a fresh superuser connection of {@code pg} (idempotent). Since P3.1
     * the engine answers a write that names an unregistered collection with the typed 422 {@code unregistered_collection},
     * so a route test registers the collections its documents name before it posts them.
     */
    public static void register(org.testcontainers.containers.PostgreSQLContainer<?> pg, String tenant, String... collections) {
        try (java.sql.Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            for (String collection : collections) {
                PgContainerHelper.insertCollection(ctx, tenant, collection);
            }
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** True when the schema carries the P3.1 id columns ({@code nexus.chunks.collection_id}, vectors-068). */
    public static boolean hasIdColumns(DSLContext ctx) {
        return PgCatalogProbes.columnExists(ctx, "nexus", "chunks", "collection_id");
    }

    /**
     * What a collection resolves to once, for a seed that writes many rows under it: the layout (partitioned or not),
     * its registered model and its registry id. Resolving per row would add four reads to every inserted chunk.
     */
    public record Registered(boolean partitioned, String model, boolean idColumns, Long id) {

        /** Resolves {@code (tenant, collection)} on {@code ctx}; an unregistered collection on a partitioned layout fails as {@code collectionModel} does. */
        public static Registered of(DSLContext ctx, String tenant, String collection) {
            if (!PgContainerHelper.isPartitionedLayout(ctx)) {
                return new Registered(false, null, false, null);
            }
            String model = PgContainerHelper.collectionModel(ctx, tenant, collection);
            boolean ids = hasIdColumns(ctx);
            return new Registered(true, model, ids, ids ? CollectionIds.require(ctx, tenant, collection) : null);
        }

        /** The key columns of a {@code nexus.chunks} seed row: tenant, collection, chash, then the model and the id where the layout has them. */
        public Map<Field<?>, Object> chunkKey(String tenant, String collection, byte[] chash) {
            var row = new LinkedHashMap<Field<?>, Object>();
            row.put(CHUNKS.TENANT_ID, tenant);
            row.put(CHUNKS.COLLECTION, collection);
            row.put(CHUNKS.CHASH, chash);
            if (partitioned) row.put(CHUNKS.EMBEDDING_MODEL, model);
            if (idColumns) row.put(CHUNKS.COLLECTION_ID, id);
            return row;
        }
    }

    /** One {@code nexus.chunks} row with text {@code "text"} and one embedding, the shape of {@code insertChunk384/768/1024}. */
    public static void insertChunk(DSLContext ctx, String tenant, String collection, byte[] chash,
                                   Field<Vector> embedding, Vector v) {
        var row = Registered.of(ctx, tenant, collection).chunkKey(tenant, collection, chash);
        row.put(CHUNKS.CHUNK_TEXT, "text");
        row.put(embedding, v);
        ctx.insertInto(CHUNKS).set(row).execute();
    }

    /** A chunk with text, one embedding and metadata, the first-write shape of {@code PgContainerHelper.insertChunks}; a repeat is left alone. */
    public static void insertChunkIfAbsent(DSLContext ctx, Registered reg, String tenant, String collection, byte[] chash,
                                           String text, Field<Vector> embedding, Vector v, JSONB metadata) {
        var row = reg.chunkKey(tenant, collection, chash);
        row.put(CHUNKS.CHUNK_TEXT, text);
        row.put(embedding, v);
        row.put(CHUNKS.METADATA, metadata);
        ctx.insertInto(CHUNKS).set(row).onConflictDoNothing().execute();
    }

    /** A reference-only chunk ({@code chunk_text} NULL); a repeat refreshes the embedding and replaces the metadata. */
    public static void upsertReferenceOnlyChunk(DSLContext ctx, String tenant, String collection, byte[] chash,
                                                Field<Vector> embedding, Vector v, JSONB metadata) {
        var row = Registered.of(ctx, tenant, collection).chunkKey(tenant, collection, chash);
        row.put(CHUNKS.CHUNK_TEXT, null);
        row.put(embedding, v);
        row.put(CHUNKS.METADATA, metadata);
        row.put(CHUNKS.RETENTION, "reference-only");
        ctx.insertInto(CHUNKS).set(row)
            .onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL)
            .doUpdate()
            .set(embedding, org.jooq.impl.DSL.excluded(embedding))
            .set(CHUNKS.METADATA, org.jooq.impl.DSL.excluded(CHUNKS.METADATA))
            .execute();
    }

    /**
     * The {@code catalog_documents} seed row that owns chunks in {@code collection}: tumbler {@code "own-" + collection},
     * {@code physical_collection} and, on an id layout, {@code physical_collection_id}. Idempotent.
     */
    public static void ownerDocument(DSLContext ctx, String tenant, String docId, String collection) {
        var doc = new LinkedHashMap<Field<?>, Object>();
        doc.put(CATALOG_DOCUMENTS.TENANT_ID, tenant);
        doc.put(CATALOG_DOCUMENTS.TUMBLER, docId);
        doc.put(CATALOG_DOCUMENTS.TITLE, "Owner " + docId);
        doc.put(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection);
        if (hasIdColumns(ctx)) {
            doc.put(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.require(ctx, tenant, collection));
        }
        ctx.insertInto(CATALOG_DOCUMENTS).set(doc).onConflictDoNothing().execute();
    }

    /**
     * One multi-row INSERT of {@code catalog_document_chunks} manifest rows for {@code docId} at successive positions
     * from {@code firstPosition}, each carrying the collection's model on a partitioned layout and its id on an id
     * layout. The chunk rows must already exist ({@code fk_catalog_chunks_chunk}).
     */
    public static void manifestRows(DSLContext ctx, String tenant, String docId, String collection, int firstPosition,
                                    String... chashHex) {
        var reg = Registered.of(ctx, tenant, collection);
        boolean partitioned = reg.partitioned();
        String model = reg.model();
        Long id = reg.id();
        var step = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS).columns(manifestColumns(partitioned, reg.idColumns()));
        for (int i = 0; i < chashHex.length; i++) {
            var values = new java.util.ArrayList<Object>(java.util.List.of(
                tenant, docId, firstPosition + i, Chash.fromHex(chashHex[i]).toBytes(), collection));
            if (partitioned) values.add(model);
            if (reg.idColumns()) values.add(id);
            step = step.values(values);
        }
        step.onConflictDoNothing().execute();
    }

    private static java.util.List<Field<?>> manifestColumns(boolean partitioned, boolean withId) {
        var cols = new java.util.ArrayList<Field<?>>(java.util.List.of(CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
            CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
            CATALOG_DOCUMENT_CHUNKS.COLLECTION));
        if (partitioned) cols.add(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL);
        if (withId) cols.add(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
        return cols;
    }

    /**
     * Registers {@code successor} if it has no registry row yet (idempotent), so a tombstone can name it: since
     * vectors-068 {@code superseded_by} and {@code superseded_by_id} are set together or not at all, and the id
     * is the successor's registry id. A seed that retired a collection under a name nothing registered
     * ({@code "elsewhere"}) now registers that name first.
     */
    public static void registerSuccessor(DSLContext ctx, String tenant, String successor) {
        PgContainerHelper.insertCollection(ctx, tenant, successor);
    }

    /**
     * Retires {@code name} as a superseded tombstone of {@code successor} the way a test seeds one with a direct row
     * write (no cache ever held it as retired): the successor is registered if needed, and {@code superseded_by} and
     * {@code superseded_by_id} are written together.
     */
    public static void supersede(DSLContext ctx, String tenant, String name, String successor) {
        registerSuccessor(ctx, tenant, successor);
        ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CollectionIds.idOf(tenant, successor))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .execute();
    }
}
