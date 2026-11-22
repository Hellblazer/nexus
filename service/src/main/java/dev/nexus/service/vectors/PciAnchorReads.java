// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

/**
 * RDR-230 Technical Design 2 (nexus-4lnn1.1.1): the reads the DDL half makes for an index's anchor, each through
 * typed jOOQ ({@code DSL.function}, {@code DSL.field(DSL.name(..))}, the shape of
 * {@code PciBuilderSession.Pass#staleLeftover} and {@code LeafBloatCatalog}), so none needs a sanction. They run on
 * the leaf's counting transaction, which has bound the tenant and a statement timeout.
 *
 * <p>Both chunk counts carry the explicit {@code tenant_id} predicate, REQUIRED for correctness for the reason
 * RDR-227 records: the admin role reads every tenant's chunks ({@code chunks_gate_probe_owner_read}, vectors-029),
 * so without it a same-named collection of another tenant would be added to the count.
 */
final class PciAnchorReads {

    private PciAnchorReads() { }

    /**
     * What the catalog says of one index right now.
     *
     * @param comment {@code obj_description}: the anchor text, or {@code null} when the index has no comment
     * @param bytes   {@code pg_relation_size} of the index
     * @param relfilenode {@code pg_class.relfilenode} of the index, which every rebuild changes; {@code null} when not
     *                known (a caller that read none), which is never evidence of a rebuild
     */
    record IndexFacts(String comment, long bytes, Long relfilenode) {
        IndexFacts(String comment, long bytes) {
            this(comment, bytes, null);
        }
    }

    /**
     * The leaf's {@code pg_stat_user_tables} row. A leaf with no row yet (never touched since the statistics were
     * reset) reads as zeros and nulls.
     */
    record LeafStats(long deleted, long updated, long hotUpdated, long live, OffsetDateTime lastVacuum,
                     OffsetDateTime lastAutovacuum) {
        static final LeafStats EMPTY = new LeafStats(0, 0, 0, 0, null, null);

        /** Updates that wrote a new index entry: the ones that can leave a dead tuple in the graph. */
        long nonHotUpdated() {
            return updated - hotUpdated;
        }
    }

    /** The anchor text and size of {@code schema.indexName}, or {@code null} when no such index exists (it went away). */
    static IndexFacts indexFacts(DSLContext tx, String schema, String indexName) {
        Field<Object> oid = DSL.field(DSL.name("c", "oid"));
        Field<String> comment = DSL.function(DSL.name("pg_catalog", "obj_description"), SQLDataType.CLOB, oid,
            DSL.inline("pg_class"));
        Field<Long> bytes = DSL.function(DSL.name("pg_catalog", "pg_relation_size"), SQLDataType.BIGINT, oid);
        Field<Long> relfilenode = DSL.field(DSL.name("c", "relfilenode")).cast(SQLDataType.BIGINT);
        Record row = tx.select(comment, bytes, relfilenode)
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(indexName))
            .and(DSL.field(DSL.name("c", "relkind"), String.class).eq("i"))
            .fetchOne();
        if (row == null || row.get(bytes) == null) {
            return null;
        }
        return new IndexFacts(row.get(comment), row.get(bytes), row.get(relfilenode));
    }

    /**
     * {@code pg_class.relfilenode} of {@code schema.indexName}, or {@code null} when no such index exists. The builder
     * reads it on its own session right before it writes an anchor, so the anchor records the storage it was written
     * against.
     */
    static Long relfilenode(DSLContext ctx, String schema, String indexName) {
        return ctx.select(DSL.field(DSL.name("c", "relfilenode")).cast(SQLDataType.BIGINT))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(indexName))
            .and(DSL.field(DSL.name("c", "relkind"), String.class).eq("i"))
            .fetchOne(0, Long.class);
    }

    /** The leaf's statistics row, or {@link LeafStats#EMPTY} when it has none. */
    static LeafStats leafStats(DSLContext tx, String schema, String leafName) {
        Field<Long> deleted = DSL.field(DSL.name("s", "n_tup_del"), SQLDataType.BIGINT);
        Field<Long> updated = DSL.field(DSL.name("s", "n_tup_upd"), SQLDataType.BIGINT);
        Field<Long> hot = DSL.field(DSL.name("s", "n_tup_hot_upd"), SQLDataType.BIGINT);
        Field<Long> live = DSL.field(DSL.name("s", "n_live_tup"), SQLDataType.BIGINT);
        Field<OffsetDateTime> vacuum = DSL.field(DSL.name("s", "last_vacuum"), SQLDataType.TIMESTAMPWITHTIMEZONE);
        Field<OffsetDateTime> autovacuum =
            DSL.field(DSL.name("s", "last_autovacuum"), SQLDataType.TIMESTAMPWITHTIMEZONE);
        Record row = tx.select(deleted, updated, hot, live, vacuum, autovacuum)
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_tables")).as("s"))
            .where(DSL.field(DSL.name("s", "schemaname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("s", "relname"), String.class).eq(leafName))
            .fetchOne();
        if (row == null) {
            return LeafStats.EMPTY;
        }
        return new LeafStats(nz(row.get(deleted)), nz(row.get(updated)), nz(row.get(hot)), nz(row.get(live)),
            row.get(vacuum), row.get(autovacuum));
    }

    /**
     * The row estimate of every table in {@code schemas}, by {@code schema.table} (the leaf key
     * {@link PciLeafReindexRule#leafId} makes): {@code max(n_live_tup, pg_class.reltuples)}, with a {@code reltuples}
     * of {@code -1} (never analyzed) read as 0. One statement for all leaves of a pass.
     *
     * <p>Both are estimates and both err low, for different reasons: {@code n_live_tup} lags the writes and is zeroed by
     * an unclean shutdown, {@code pg_stat_reset}, a restore or a major upgrade (it stays 0 until a vacuum or analyze
     * runs, which a quiet leaf may never get); {@code reltuples} survives all of those but is only as fresh as the last
     * vacuum or analyze. The larger of the two is the better floor, as {@code LeafBloatCatalog} reads "rows"
     * (nexus-dxsjc). The reads are catalog and statistics views; they lock no relation.
     */
    static java.util.Map<String, Long> leafRowEstimates(DSLContext tx, java.util.Collection<String> schemas) {
        Field<String> schema = DSL.field(DSL.name("s", "schemaname"), String.class);
        Field<String> table = DSL.field(DSL.name("s", "relname"), String.class);
        Field<Long> live = DSL.field(DSL.name("s", "n_live_tup"), SQLDataType.BIGINT);
        Field<Double> planner = DSL.field(DSL.name("c", "reltuples")).cast(SQLDataType.DOUBLE);
        java.util.Map<String, Long> out = new java.util.HashMap<>();
        for (Record row : tx.select(schema, table, live, planner)
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_user_tables")).as("s"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("s", "relid"))))
                .where(schema.in(schemas)).fetch()) {
            Double est = row.get(planner);
            long fromPlanner = est == null || est < 0 ? 0 : Math.round(est);
            out.put(row.get(schema) + "." + row.get(table), Math.max(nz(row.get(live)), fromPlanner));
        }
        return out;
    }

    /**
     * Every row of {@code collection} on the leaf of ({@code model}, {@code tenant}), uncapped: rows present in the
     * table, not RDR-192's owned-chunk sense of live. Its cost is an exact scan of the collection's rows (0.3 to 0.6 s
     * at 60k, T2 {@code nexus_rdr/230-gate-latest}).
     */
    static long countRows(DSLContext tx, int dim, String model, String tenant, String collection) {
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        Long n = tx.select(DSL.count()).from(ch.table())
            .where(ch.embeddingModel().eq(model))
            .and(ch.tenantId().eq(tenant))
            .and(ch.collection().eq(collection))
            .fetchOne(0, Long.class);
        return n == null ? 0 : n;
    }

    /**
     * Rows of {@code collection} whose {@code last_written_at} is later than {@code builtAt}: the graph entries the
     * collection gained since the build ({@code last_written_at} is set on insert and refreshed by every client
     * re-write of a chunk, never by a maintenance update: vectors-020).
     */
    static long writtenSince(DSLContext tx, int dim, String model, String tenant, String collection,
                             Instant builtAt) {
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        Long n = tx.select(DSL.count()).from(ch.table())
            .where(ch.embeddingModel().eq(model))
            .and(ch.tenantId().eq(tenant))
            .and(ch.collection().eq(collection))
            .and(ch.lastWrittenAt().gt(OffsetDateTime.ofInstant(builtAt, ZoneOffset.UTC)))
            .fetchOne(0, Long.class);
        return n == null ? 0 : n;
    }

    /** {@link #countRows} for the id predicate (RDR-229 P3.4): every row with this {@code collection_id} on the leaf of (model, tenant). */
    static long countRowsById(DSLContext tx, String model, String tenant, long collectionId) {
        Long n = tx.select(DSL.count()).from(dev.nexus.service.jooq.nexus.Tables.CHUNKS)
            .where(dev.nexus.service.jooq.nexus.Tables.CHUNKS.EMBEDDING_MODEL.eq(model))
            .and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.TENANT_ID.eq(tenant))
            .and(dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION_ID.eq(collectionId))
            .fetchOne(0, Long.class);
        return n == null ? 0 : n;
    }

    /** {@link #writtenSince} for the id predicate. */
    static long writtenSinceById(DSLContext tx, String model, String tenant, long collectionId, Instant builtAt) {
        var chunks = dev.nexus.service.jooq.nexus.Tables.CHUNKS;
        Long n = tx.select(DSL.count()).from(chunks)
            .where(chunks.EMBEDDING_MODEL.eq(model))
            .and(chunks.TENANT_ID.eq(tenant))
            .and(chunks.COLLECTION_ID.eq(collectionId))
            .and(chunks.LAST_WRITTEN_AT.gt(OffsetDateTime.ofInstant(builtAt, ZoneOffset.UTC)))
            .fetchOne(0, Long.class);
        return n == null ? 0 : n;
    }

    private static long nz(Long v) {
        return v == null ? 0 : v;
    }
}
