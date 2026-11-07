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
     */
    record IndexFacts(String comment, long bytes) { }

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
        Record row = tx.select(comment, bytes)
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
        return new IndexFacts(row.get(comment), row.get(bytes));
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

    private static long nz(Long v) {
        return v == null ? 0 : v;
    }
}
