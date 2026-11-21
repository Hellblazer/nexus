// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.types.DayToSecond;
import org.jooq.types.YearToMonth;
import org.jooq.types.YearToSecond;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_IS_REAPABLE;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Fixture shared by the tests of the orphaning record: one Postgres container with the product schema and the
 * RLS-subject service role, and the helpers that seed chunks, documents and manifest rows through direct
 * substrate SQL (the write routes refuse an ownerless write since RDR-223 Phase 3).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
abstract class ChunkReapableTestBase {

    protected static final String SVC_ROLE = "svc_reapable_test";
    protected static final String SVC_PASS = "svc_reapable_test_pass";

    protected static final String KNOWLEDGE = "knowledge__reap-k__minilm-l6-v2-384__v1";
    protected static final String DOCS = "docs__reap-d__minilm-l6-v2-384__v1";
    protected static final String CODE = "code__reap-c__minilm-l6-v2-384__v1";
    protected static final String RDR = "rdr__reap-r__minilm-l6-v2-384__v1";
    protected static final String OTHER = "knowledge__reap-o__minilm-l6-v2-384__v1";

    protected static final Duration DAYS_40 = Duration.ofDays(40);

    protected PostgreSQLContainer<?> pg;
    protected HikariDataSource svcDs;
    protected TenantScope tenantScope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    protected static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    protected static byte[] bytes(String chashHex) {
        return Chash.fromHex(chashHex).toBytes();
    }

    protected void su(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** {@link #su} in ONE transaction: a manifest replace is a DELETE and an INSERT that commit together. */
    protected void suTxn(java.util.function.Consumer<DSLContext> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(false);
            work.accept(DSL.using(c, SQLDialect.POSTGRES));
            c.commit();
        }
    }

    protected void register(String tenant, String collection) throws Exception {
        su(ctx -> PgContainerHelper.insertCollection(ctx, tenant, collection));
    }

    /** Inserts one chunk through substrate SQL, last written {@code age} ago. No manifest row. */
    protected String orphan(String tenant, String collection, String seed, Duration age) throws Exception {
        String hex = ch(tenant + "/" + collection + "/" + seed);
        su(ctx -> {
            PgContainerHelper.insertChunks(ctx, tenant, collection, List.of(hex), List.of(seed + " text"),
                List.of(new float[384]), List.of(Map.of()));
            ageTo(ctx, tenant, collection, hex, age);
        });
        return hex;
    }

    /** Makes the chunk old in every respect: its write time, and its orphaning record if it has one. */
    protected static void ageTo(DSLContext ctx, String tenant, String collection, String hex, Duration age) {
        OffsetDateTime then = OffsetDateTime.now().minus(age);
        ctx.update(CHUNKS).set(CHUNKS.CREATED_AT, then).set(CHUNKS.LAST_WRITTEN_AT, then)
           .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(bytes(hex))))
           .execute();
        ctx.update(CHUNK_ORPHANED_AT).set(CHUNK_ORPHANED_AT.ORPHANED_AT, then)
           .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection))
                  .and(CHUNK_ORPHANED_AT.CHASH.eq(bytes(hex))))
           .execute();
    }

    /** Writes (or overwrites) the chunk's orphaning record, {@code age} ago. A fixture for the predicate's tests. */
    protected void recordOrphaning(String tenant, String collection, String hex, Duration age) throws Exception {
        OffsetDateTime then = OffsetDateTime.now().minus(age);
        // RDR-225: the record carries its chunk's embedding model (the four-column chunk foreign key).
        su(ctx -> ctx.insertInto(CHUNK_ORPHANED_AT)
            .set(CHUNK_ORPHANED_AT.TENANT_ID, tenant).set(CHUNK_ORPHANED_AT.COLLECTION, collection)
            .set(CHUNK_ORPHANED_AT.COLLECTION_ID, CollectionIds.idOf(tenant, collection))
            .set(CHUNK_ORPHANED_AT.CHASH, bytes(hex)).set(CHUNK_ORPHANED_AT.ORPHANED_AT, then)
            .set(CHUNK_ORPHANED_AT.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, tenant, collection))
            .onConflict(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH)
            .doUpdate().set(CHUNK_ORPHANED_AT.ORPHANED_AT, then).execute());
    }

    protected static YearToSecond interval(Duration d) {
        if (d == null) return null;
        return new YearToSecond(new YearToMonth(0, 0), DayToSecond.valueOf(d));
    }

    /** The one call shape every consumer uses: EXISTS over the function, correlated on the chunk row. */
    protected static org.jooq.Condition reapable(Duration grace) {
        return DSL.exists(DSL.selectFrom(CHUNK_IS_REAPABLE.call(
            CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.LAST_WRITTEN_AT,
            DSL.val(interval(grace), SQLDataType.INTERVAL))));
    }

    /** True when {@code reapable(c)} holds for the chunk, evaluated as the RLS-subject service role. */
    protected boolean isReapable(String tenant, String collection, String hex, Duration grace) {
        return tenantScope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(CHUNKS)
               .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection))
                      .and(CHUNKS.CHASH.eq(bytes(hex))))
               .and(reapable(grace))));
    }

    protected boolean isReapable(String tenant, String collection, String hex) {
        return isReapable(tenant, collection, hex, null);
    }

    protected void document(String tenant, String tumbler, String collection, boolean tombstoned) throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, "doc " + tumbler).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(tenant, collection))
            .set(CATALOG_DOCUMENTS.DELETED_AT, tombstoned ? OffsetDateTime.now().minusDays(1) : null)
            .onConflictDoNothing().execute());
    }

    protected void manifestRow(String tenant, String docId, String collection, String hex, int position)
            throws Exception {
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position).set(CATALOG_DOCUMENT_CHUNKS.CHASH, bytes(hex))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CollectionIds.idOf(tenant, collection))
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, PgContainerHelper.collectionModel(ctx, tenant, collection))
            .execute());
    }

    protected OffsetDateTime lastWrittenAt(String tenant, String collection, String hex) throws Exception {
        OffsetDateTime[] out = new OffsetDateTime[1];
        su(ctx -> out[0] = ctx.select(CHUNKS.LAST_WRITTEN_AT).from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(bytes(hex))))
            .fetchOne(0, OffsetDateTime.class));
        return out[0];
    }

    /** The chunk's orphaning record, or null when it has none. */
    protected OffsetDateTime orphanedAt(String tenant, String collection, String hex) throws Exception {
        OffsetDateTime[] out = new OffsetDateTime[1];
        su(ctx -> out[0] = ctx.select(CHUNK_ORPHANED_AT.ORPHANED_AT).from(CHUNK_ORPHANED_AT)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection))
                   .and(CHUNK_ORPHANED_AT.CHASH.eq(bytes(hex))))
            .fetchOne(0, OffsetDateTime.class));
        return out[0];
    }

    /** True when the chunk has an orphaning record written within the last ten minutes. */
    protected boolean orphanRecordedRecently(String tenant, String collection, String hex) throws Exception {
        OffsetDateTime at = orphanedAt(tenant, collection, hex);
        return at != null && at.isAfter(OffsetDateTime.now().minusMinutes(10));
    }

    protected long recordCount(String tenant) throws Exception {
        long[] n = new long[1];
        su(ctx -> n[0] = ctx.fetchCount(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant)));
        return n[0];
    }

    protected String functionDefinition(String signature) throws Exception {
        String[] out = new String[1];
        su(ctx -> out[0] = PgCatalogProbes.routineDefinition(ctx, signature));
        assertThat(out[0]).as("%s must resolve", signature).isNotNull();
        return out[0];
    }

    protected static int occurrences(String haystack, String needle) {
        int n = 0;
        for (int i = haystack.indexOf(needle); i >= 0; i = haystack.indexOf(needle, i + 1)) n++;
        return n;
    }
}
