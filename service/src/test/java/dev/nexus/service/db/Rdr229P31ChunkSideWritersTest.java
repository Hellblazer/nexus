// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.1 stage 2b, chunks family (nexus-p2m1x.17): the two chunk UPDATE writers outside {@code PgVectorRepository}
 * that live in package {@code db}, {@link QuarantineOrigin#retagRowsOf} and {@link DepartedWriterSpans#clear}.
 *
 * <p>Each rewrites chunk rows without naming the collection, so on a chunk the P3.1 stamp has not reached (collection
 * set, {@code collection_id} NULL) the NOT VALID CHECK refuses the UPDATE unless the statement sets the id (23514).
 * {@code DepartedWriterSpans.clear} is fail-open, so the refusal would not even surface: the spans would stay and the
 * clear would report zero. The retag also gains the id of the origin it names, {@code origin_collection_id}, a metadata
 * key beside {@code origin_collection} (the tag the engine's move writes), so a reader can follow a rename by id.
 * The unstamped state is built with the changeset's own {@code <rollback>} and forward text, as the upgrade has it.
 */
class Rdr229P31ChunkSideWritersTest extends AtomicWriteTestBase {

    private static final String CHANGELOG = "db/changelog/vectors-068-collection-id-expand.xml";
    private static final String CHUNK_CHECKS = "vectors-068-3";
    private static final String T = "p31-side-writers";
    private static final List<String> SPAN_KEYS = List.of("line_start", "line_end", "chunk_start_char", "chunk_end_char");

    private String col(String tag) {
        return "knowledge__p31s" + tag + seq.incrementAndGet() + "__minilm-l6-v2-384__v1";
    }

    private void register(String name, String state) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, T, name);
            ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, state)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T).and(CATALOG_COLLECTIONS.NAME.eq(name))).execute();
        }
    }

    private Long registryId(String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
        }
    }

    private void unstamp(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.replayChangesetRollback(su, CHANGELOG, CHUNK_CHECKS);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).setNull(CHUNKS.COLLECTION_ID)
                .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(collection)).execute();
            PgContainerHelper.replayChangeset(su, CHANGELOG, CHUNK_CHECKS);
        }
    }

    private record Row(Long collectionId, String metadata) {}

    private Row row(String collection, String hex) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.COLLECTION_ID, CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hex))).fetchOne();
            JSONB meta = r.value2();
            return new Row(r.value1(), meta == null ? null : meta.data());
        }
    }

    // ---- QuarantineOrigin.retagRowsOf --------------------------------------------------------------------------

    @Test
    void theRetagTagsTheOriginByNameAndId_andStampsAnUnstampedQuarantineRow() throws Exception {
        String from = col("from");
        String to = col("to");
        String sibling = "quarantine-" + from;
        register(from, "live");
        register(to, "live");
        register(sibling, "quarantine");
        String hex = Chash.ofText("retag " + seq.get()).toHex();
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("origin_collection", from);
        meta.put("quarantined_at", "2026-01-02T00:00:00Z");
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), T, sibling, List.of(hex),
                List.of("retag text"), List.of(new float[384]), List.of(meta));
        }
        unstamp(sibling);
        assertThat(row(sibling, hex).collectionId()).as("fixture: unstamped").isNull();

        int n = tenantScope.withTenant(T, ctx -> QuarantineOrigin.retagRowsOf(ctx, T, from, to, Gate3.never()));

        assertThat(n).as("one row retagged").isEqualTo(1);
        Row after = row(sibling, hex);
        assertThat(after.collectionId()).as("the UPDATE sets the id of the row's own collection").isEqualTo(registryId(sibling));
        assertThat(after.metadata()).contains("\"origin_collection\": \"" + to + "\"");
        assertThat(after.metadata()).as("origin_collection_id is the registry id of the new origin, a number")
            .contains("\"origin_collection_id\": " + registryId(to));
        assertThat(after.metadata()).as("the other keys the move wrote survive").contains("quarantined_at");
    }

    /**
     * linda (census reconciliation, 2026-11-23): before the latch the retag stamps a NULL id with the sibling's, and keeps a stored one. A row whose stored
     * id is another registered row's (the label was re-registered, or the row was stamped before a rename) must not be rewritten to what the label resolves
     * to now: {@code DepartedWriterSpans} dropped that shape, and {@code retagRowsOf} keeps the stored id with a COALESCE.
     */
    @Test
    void theRetagKeepsAStoredIdThatDiffersFromTheSiblingsCurrentOne_andStampsOnlyANullId() throws Exception {
        String from = col("from");
        String to = col("to");
        String other = col("other");
        String sibling = "quarantine-" + from;      // its chunk's stored id will be OTHER's
        String nullSibling = col("nullsib");        // its chunk is unstamped
        register(from, "live");
        register(to, "live");
        register(other, "live");
        register(sibling, "quarantine");
        register(nullSibling, "quarantine");
        String keptHex = Chash.ofText("retag-keep " + seq.get()).toHex();
        String nullHex = Chash.ofText("retag-null " + seq.get()).toHex();
        try (Connection su = pg.createConnection("")) {
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertChunks(d, T, sibling, List.of(keptHex), List.of("keep text"), List.of(new float[384]), List.of(Map.of("origin_collection", from)));
            PgContainerHelper.insertChunks(d, T, nullSibling, List.of(nullHex), List.of("null text"), List.of(new float[384]), List.of(Map.of("origin_collection", from)));
        }
        unstamp(nullSibling);
        long otherId = registryId(other);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CHUNKS).set(CHUNKS.COLLECTION_ID, otherId)
                .where(CHUNKS.TENANT_ID.eq(T)).and(CHUNKS.COLLECTION.eq(sibling)).execute();
        }
        assertThat(row(sibling, keptHex).collectionId()).as("fixture: the stored id is another row's").isEqualTo(otherId).isNotEqualTo(registryId(sibling));
        assertThat(row(nullSibling, nullHex).collectionId()).as("fixture: the other row is unstamped").isNull();

        int n = tenantScope.withTenant(T, ctx -> QuarantineOrigin.retagRowsOf(ctx, T, from, to, Gate3.never()));

        assertThat(n).as("both rows retagged").isEqualTo(2);
        Row kept = row(sibling, keptHex);
        assertThat(kept.metadata()).as("the tag was retagged").contains("\"origin_collection\": \"" + to + "\"");
        assertThat(kept.collectionId()).as("the stored id is kept, not rewritten to the sibling's current one").isEqualTo(otherId);
        Row stamped = row(nullSibling, nullHex);
        assertThat(stamped.metadata()).contains("\"origin_collection\": \"" + to + "\"");
        assertThat(stamped.collectionId()).as("a NULL id is stamped with the sibling's").isEqualTo(registryId(nullSibling));
    }

    @Test
    void theRetagOfAnOriginWithNoRegistryRowStillTagsByName_andLeavesTheIdOut() throws Exception {
        String from = col("from");
        String to = col("unregistered");
        String sibling = "quarantine-" + from;
        register(from, "live");
        register(sibling, "quarantine");
        String hex = Chash.ofText("retag-unreg " + seq.get()).toHex();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertChunks(DSL.using(su, SQLDialect.POSTGRES), T, sibling, List.of(hex),
                List.of("retag text"), List.of(new float[384]), List.of(Map.of("origin_collection", from)));
        }

        int n = tenantScope.withTenant(T, ctx -> QuarantineOrigin.retagRowsOf(ctx, T, from, to, Gate3.never()));

        assertThat(n).isEqualTo(1);
        Row after = row(sibling, hex);
        assertThat(after.metadata()).contains("\"origin_collection\": \"" + to + "\"").doesNotContain("origin_collection_id");
        assertThat(after.collectionId()).isEqualTo(registryId(sibling));
    }

    // ---- DepartedWriterSpans.clear -----------------------------------------------------------------------------

    @Test
    void clearingASpanFromAnUnstampedHolderChunkStampsItsId() throws Exception {
        String c = col("span");
        register(c, "live");
        String hex = Chash.ofText("span clear " + seq.get()).toHex();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext d = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertChunks(d, T, c, List.of(hex), List.of("span text"), List.of(new float[384]),
                List.of(Map.of("line_start", 500, "line_end", 510, "chunk_start_char", 5000, "chunk_end_char", 5100)));
            PgContainerHelper.ownChunks(d, T, c, hex);   // a live holder with no span of its own
        }
        unstamp(c);
        var gone = List.of(new DepartedWriterSpans.Gone(c, HexFormat.of().parseHex(hex), null, null, null, null));

        int cleared = tenantScope.withTenant(T, ctx -> DepartedWriterSpans.clear(ctx, T, gone, Gate3.never()));

        assertThat(cleared).as("fail-open: a refused UPDATE would read 0 here").isEqualTo(1);
        Row after = row(c, hex);
        for (String k : SPAN_KEYS) {
            assertThat(after.metadata()).as("the span key %s was cleared", k).doesNotContain("\"" + k + "\"");
        }
        assertThat(after.collectionId()).isEqualTo(registryId(c));
    }
}
