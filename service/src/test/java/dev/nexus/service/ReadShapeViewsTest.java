// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.jooq.DSLContext;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_STATS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_DOC_COUNTS;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_HEALTH_META;
import static dev.nexus.service.jooq.nexus.Tables.COVERAGE_BY_CONTENT_TYPE;
import static dev.nexus.service.jooq.nexus.Tables.LINKS_BY_TYPE_COUNTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS_WITH_COUNTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-154 P1.2 (bead nexus-h9qyp) — security_invoker read-shape views.
 *
 * <p>Two guarantees, mirroring CollectionVectorStatsTest GROUP 3 + GROUP 5:
 * <ul>
 *   <li>Every one of the five views has {@code security_invoker=true} PHYSICALLY
 *       set in pg_class.reloptions (a comment / a changelog-grep is not proof
 *       that Liquibase actually applied it).</li>
 *   <li>Cross-tenant isolation under a NOSUPERUSER NOBYPASSRLS svc role + GUC:
 *       the grouped views (which carry tenant_id) leak ZERO foreign-tenant rows,
 *       and the scalar catalog_stats view scopes its counts to the GUC tenant.
 *       A superuser CONTROL proves foreign rows exist underneath (non-vacuous).</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ReadShapeViewsTest {

    private static final String TENANT_A = "rsv-tenant-a";
    private static final String TENANT_B = "rsv-tenant-b";
    private static final String SVC_ROLE = "svc_rsv_test";
    private static final String SVC_PASS = "svc_rsv_test_pass";

    private static final List<String> VIEWS = List.of(
        "catalog_stats", "collection_doc_counts", "coverage_by_content_type",
        "collection_health_meta", "topics_with_counts", "links_by_type_counts");

    // The views that carry a tenant_id column (catalog_stats is scalar).
    private static final List<String> GROUPED_VIEWS = List.of(
        "collection_doc_counts", "coverage_by_content_type",
        "collection_health_meta", "topics_with_counts", "links_by_type_counts");

    // Name -> generated jOOQ Table, for the loop over GROUPED_VIEWS below: each view
    // has a different generated Table class but all five carry a TENANT_ID column,
    // resolved generically via Table#field(String, Class) (nexus-cbo4a batch 10).
    private static final Map<String, Table<?>> GROUPED_VIEW_TABLES = Map.of(
        "collection_doc_counts", COLLECTION_DOC_COUNTS,
        "coverage_by_content_type", COVERAGE_BY_CONTENT_TYPE,
        "collection_health_meta", COLLECTION_HEALTH_META,
        "topics_with_counts", TOPICS_WITH_COUNTS,
        "links_by_type_counts", LINKS_BY_TYPE_COUNTS);

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // Fixtures chosen so EVERY catalog_stats scalar differs A vs B (non-vacuous
            // per-subquery RLS scoping) AND every grouped view has rows for both
            // tenants. TENANT_A: 2 docs, 1 link, 2 owners, 2 collections, 2 chunks,
            // 1 topic. TENANT_B: 1 doc, 2 links, 1 owner, 1 collection, 1 chunk, 1 topic.
            //
            // nexus-tk070.p1 (RDR-194 § D2): TENANT_B's two links used to point at
            // "dangling tumblers" (b.x1/b.x2, never registered as documents) on the
            // strength of the pre-FK comment "links are not FK-enforced" — that is no
            // longer true (fk_catalog_links_from_document/_to_document), so a link to a
            // nonexistent tumbler is a hard INSERT failure now, even via raw SQL. Fixed
            // by self-linking b.1 -> b.1 under two different link_types (the unique key
            // is (tenant_id, from_tumbler, to_tumbler, link_type), so two rows are still
            // distinct) rather than registering b.x1/b.x2 as real documents, which would
            // have inflated TENANT_B's doc_count from 1 to 3 and broken
            // catalogStats_scopesScalarCountsToGucTenant's GUC=B doc_count==1 pin below.
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedDoc(ctx, TENANT_A, "a.1", "paper", "c_a");
            seedDoc(ctx, TENANT_A, "a.2", "code",  "c_a");
            ctx.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER,
                    CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
                .values(TENANT_A, "a.1", "a.2", "cites", "test")
                .execute();
            seedTopic(ctx, TENANT_A, "topic-a", "c_a");
            seedOwner(ctx, TENANT_A, "a-own-1");
            seedOwner(ctx, TENANT_A, "a-own-2");
            seedColl(ctx, TENANT_A, "c_a");
            seedColl(ctx, TENANT_A, "c_a2");
            seedChunk(ctx, TENANT_A, "a.1", 0, "chash-a-0", "c_a");
            seedChunk(ctx, TENANT_A, "a.1", 1, "chash-a-1", "c_a");

            seedDoc(ctx, TENANT_B, "b.1", "paper", "c_b");
            ctx.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER,
                    CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
                .values(TENANT_B, "b.1", "b.1", "cites", "test")
                .values(TENANT_B, "b.1", "b.1", "relates", "test")
                .execute();
            seedTopic(ctx, TENANT_B, "topic-b", "c_b");
            seedOwner(ctx, TENANT_B, "b-own-1");
            seedColl(ctx, TENANT_B, "c_b");
            seedChunk(ctx, TENANT_B, "b.1", 0, "chash-b-0", "c_b");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static void seedDoc(DSLContext ctx, String tenant, String tumbler,
                                String ctype, String coll) {
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                CATALOG_DOCUMENTS.INDEXED_AT)
            .values(tenant, tumbler, "T", ctype, coll, OffsetDateTime.parse("2026-01-01T00:00:00Z"))
            .execute();
    }

    private static void seedTopic(DSLContext ctx, String tenant, String label, String coll) {
        // RDR-164 P1a: register the collection (topics_collection_fk).
        PgContainerHelper.insertCollection(ctx, tenant, coll);
        ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(tenant, label, coll, 0, OffsetDateTime.now(), "pending")
            .execute();
    }

    private static void seedDocIndexed(DSLContext ctx, String tenant, String tumbler,
                                       String coll, String indexedAt) {
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.INDEXED_AT)
            .values(tenant, tumbler, "T", coll, indexedAt == null ? null : OffsetDateTime.parse(indexedAt))
            .execute();
    }

    @Test @Order(40)
    void collectionHealthMeta_staleSourceRatio_indexAge() throws Exception {
        // nexus-agsq7: stale = indexed_at more than 30 days ago. 2020 is always
        // stale, 2099 is always fresh (future) — deterministic regardless of now().
        final String col = "c_stale_age";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedDocIndexed(ctx, TENANT_A, "sa.1", col, "2020-01-01T00:00:00Z"); // stale
            seedDocIndexed(ctx, TENANT_A, "sa.2", col, "2099-01-01T00:00:00Z"); // fresh
            Double staleRatio = ctx.select(COLLECTION_HEALTH_META.STALE_SOURCE_RATIO).from(COLLECTION_HEALTH_META)
                .where(COLLECTION_HEALTH_META.TENANT_ID.eq(TENANT_A))
                .and(COLLECTION_HEALTH_META.COLLECTION.eq(col))
                .fetchOne(COLLECTION_HEALTH_META.STALE_SOURCE_RATIO);
            assertThat(staleRatio)
                .as("1 of 2 dated docs is > 30 days old").isEqualTo(0.5d);
        }
    }

    /**
     * nexus-cefa1.2: collection_health_meta PARITY test for catalog-031-1-documents-
     * temporal's DROP/CREATE (regex-guard + to_char lexicographic hacks retired to plain
     * timestamptz comparisons). Asserts the view's MAX(indexed_at)/last_indexed and
     * orphan_count semantics are UNCHANGED against the same shape of fixture the
     * pre-migration view handled: an undated (NULL indexed_at) document must be excluded
     * from both last_indexed and the stale_source_ratio denominator — mirroring the old
     * regex guard's exclusion of a non-ISO-prefixed indexed_at — while still counting
     * toward orphan_count (orphan-ness is independent of indexed_at entirely).
     */
    @Test @Order(41)
    void collectionHealthMeta_parity_maxIndexedAtAndUndatedDocExclusion() throws Exception {
        final String col = "c_chm_parity";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedDocIndexed(ctx, TENANT_A, "chmp.1", col, "2026-01-01T08:00:00Z");
            seedDocIndexed(ctx, TENANT_A, "chmp.2", col, "2026-06-01T12:00:00Z");
            seedDocIndexed(ctx, TENANT_A, "chmp.3", col, "2026-03-15T00:00:00Z");
            // Undated doc (NULL indexed_at, the '' -> NULL post-migration shape) — must
            // NOT win the MAX and must NOT count toward the stale_source_ratio denominator.
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                    CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                .values(TENANT_A, "chmp.4", "T", col)
                .execute();

            var row = ctx.select(COLLECTION_HEALTH_META.LAST_INDEXED, COLLECTION_HEALTH_META.ORPHAN_COUNT,
                    COLLECTION_HEALTH_META.STALE_SOURCE_RATIO)
                .from(COLLECTION_HEALTH_META)
                .where(COLLECTION_HEALTH_META.TENANT_ID.eq(TENANT_A))
                .and(COLLECTION_HEALTH_META.COLLECTION.eq(col))
                .fetchOptional();
            assertThat(row.isPresent()).isTrue();
            assertThat(row.get().value1().toInstant())
                .as("MAX(indexed_at) over {01-01, 06-01, 03-15, NULL} must be 06-01, "
                    + "the NULL undated doc must not participate")
                .isEqualTo(java.time.Instant.parse("2026-06-01T12:00:00Z"));
            assertThat(row.get().value2())
                .as("all 4 docs (dated and undated alike) have no inbound link — orphan-ness "
                    + "does not depend on indexed_at")
                .isEqualTo(4L);
            // 3 dated docs, all far in the past relative to any real test-run clock — all
            // stale; the undated 4th doc excluded from BOTH numerator and denominator.
            assertThat(row.get().value3())
                .as("3/3 dated docs are stale; the undated 4th doc must not water down the ratio")
                .isEqualTo(1.0d);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // TOMBSTONE AUDIT 2026-08-01 (nexus-se9r3, nexus-l1nre, nexus-cah9n)
    // ══════════════════════════════════════════════════════════════════════════

    private record StatsRow(long docCount, long chunkCount) {}

    private StatsRow statsFor(String tenant) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            var row = DSL.using(svc, SQLDialect.POSTGRES)
                .select(CATALOG_STATS.DOC_COUNT, CATALOG_STATS.CHUNK_COUNT).from(CATALOG_STATS)
                .fetchOne();
            return new StatsRow(row.value1(), row.value2());
        }
    }

    /**
     * nexus-se9r3: doc_count filters deleted_at IS NULL, and chunk_count
     * counts only chunks whose OWNING document is live (soft-delete does not
     * cascade to catalog_document_chunks, so the naive raw count would keep
     * counting a tombstoned doc's chunks). Superuser CONTROL at the end
     * proves the tombstoned row and its chunk exist underneath — this is a
     * real exclusion, not an artifact of the row never having existed.
     */
    @Test @Order(50)
    void catalogStats_docCountAndChunkCount_excludeTombstonedDocs() throws Exception {
        String tenant = "rsv-tomb-stats-" + System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedColl(ctx, tenant, "c_ts_tomb");
            seedDoc(ctx, tenant, "ts.live", "paper", "c_ts_tomb");
            seedDoc(ctx, tenant, "ts.dead", "paper", "c_ts_tomb");
            seedChunk(ctx, tenant, "ts.live", 0, "chash-ts-live", "c_ts_tomb");
            seedChunk(ctx, tenant, "ts.dead", 0, "chash-ts-dead", "c_ts_tomb");
        }

        StatsRow before = statsFor(tenant);
        assertThat(before.docCount()).as("both docs counted while live").isEqualTo(2L);
        assertThat(before.chunkCount()).as("both chunks counted while owning doc is live").isEqualTo(2L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("ts.dead"))
                .execute();
        }

        StatsRow after = statsFor(tenant);
        assertThat(after.docCount()).as("doc_count drops by exactly the tombstoned doc").isEqualTo(1L);
        assertThat(after.chunkCount())
            .as("chunk_count drops too — the manifest does not cascade the tombstone, "
                + "but chunk_count now counts only chunks of LIVE documents")
            .isEqualTo(1L);

        // CONTROL: the tombstoned row and its manifest chunk both still exist underneath.
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            int docCount = ctx.selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("ts.dead"))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())
                .fetchOne(0, int.class);
            assertThat(docCount).as("CONTROL: tombstoned row exists").isEqualTo(1);
            int chunkCount = ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("ts.dead"))
                .fetchOne(0, int.class);
            assertThat(chunkCount)
                .as("CONTROL: the tombstoned doc's manifest chunk row survives (no cascade)")
                .isEqualTo(1);
        }
    }

    private long collectionDocCount(String tenant, String coll) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            Long count = DSL.using(svc, SQLDialect.POSTGRES)
                .select(COLLECTION_DOC_COUNTS.DOC_COUNT).from(COLLECTION_DOC_COUNTS)
                .where(COLLECTION_DOC_COUNTS.PHYSICAL_COLLECTION.eq(coll))
                .fetchOne(COLLECTION_DOC_COUNTS.DOC_COUNT);
            return count == null ? 0L : count;
        }
    }

    /** nexus-se9r3: collection_doc_counts excludes tombstoned documents. */
    @Test @Order(51)
    void collectionDocCounts_excludesTombstonedDocs() throws Exception {
        String tenant = "rsv-tomb-cdc-" + System.nanoTime();
        String coll = "c_cdc_tomb";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedColl(ctx, tenant, coll);
            seedDoc(ctx, tenant, "cdc.live", "paper", coll);
            seedDoc(ctx, tenant, "cdc.dead", "paper", coll);
        }
        assertThat(collectionDocCount(tenant, coll)).isEqualTo(2L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("cdc.dead"))
                .execute();
        }
        assertThat(collectionDocCount(tenant, coll))
            .as("tombstoned doc no longer counted").isEqualTo(1L);

        try (Connection su = pg.createConnection("")) {
            int count = DSL.using(su, SQLDialect.POSTGRES).selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("cdc.dead"))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())
                .fetchOne(0, int.class);
            assertThat(count).as("CONTROL: tombstoned row exists").isEqualTo(1);
        }
    }

    private long coverageTotalFor(String tenant, String contentType) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            Long total = DSL.using(svc, SQLDialect.POSTGRES)
                .select(COVERAGE_BY_CONTENT_TYPE.TOTAL).from(COVERAGE_BY_CONTENT_TYPE)
                .where(COVERAGE_BY_CONTENT_TYPE.CONTENT_TYPE.eq(contentType))
                .fetchOne(COVERAGE_BY_CONTENT_TYPE.TOTAL);
            return total == null ? 0L : total;
        }
    }

    /**
     * nexus-l1nre: coverage_by_content_type excludes tombstoned documents.
     * Before this fix the view branch of coverageByContentType (empty
     * ownerPrefix) was the ONLY unfiltered branch — the owner-prefix hand
     * aggregation already filtered deleted_at IS NULL — so the same method
     * disagreed with itself depending on whether a prefix was supplied
     * (CatalogRepositoryTest pins the two branches' agreement directly).
     */
    @Test @Order(52)
    void coverageByContentType_excludesTombstonedDocs() throws Exception {
        String tenant = "rsv-tomb-cov-" + System.nanoTime();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedDoc(ctx, tenant, "cov.live", "paper", "c_cov_tomb");
            seedDoc(ctx, tenant, "cov.dead", "paper", "c_cov_tomb");
        }
        assertThat(coverageTotalFor(tenant, "paper")).isEqualTo(2L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("cov.dead"))
                .execute();
        }
        assertThat(coverageTotalFor(tenant, "paper"))
            .as("tombstoned doc excluded from total").isEqualTo(1L);

        try (Connection su = pg.createConnection("")) {
            int count = DSL.using(su, SQLDialect.POSTGRES).selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("cov.dead"))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())
                .fetchOne(0, int.class);
            assertThat(count).as("CONTROL: tombstoned row exists").isEqualTo(1);
        }
    }

    private long orphanCountFor(String tenant, String coll) throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            Long count = DSL.using(svc, SQLDialect.POSTGRES)
                .select(COLLECTION_HEALTH_META.ORPHAN_COUNT).from(COLLECTION_HEALTH_META)
                .where(COLLECTION_HEALTH_META.COLLECTION.eq(coll))
                .fetchOne(COLLECTION_HEALTH_META.ORPHAN_COUNT);
            return count == null ? 0L : count;
        }
    }

    /**
     * nexus-cah9n: a tombstoned document must not inflate orphan_count.
     * Before this fix, a tombstoned doc stayed in the GROUP BY scope (only
     * physical_collection IS NOT NULL was filtered) and — having no inbound
     * links of its own — always fell into the orphan FILTER, so deleting
     * documents RAISED the reported orphan count. After the fix, a
     * tombstoned doc leaves collection_health_meta's scope ENTIRELY: chm.a
     * starts as a genuine orphan (no inbound link) and, once tombstoned,
     * must drop OUT of the count rather than staying counted.
     */
    @Test @Order(53)
    void collectionHealthMeta_tombstonedDoc_leavesOrphanScopeEntirely() throws Exception {
        String tenant = "rsv-tomb-chm-" + System.nanoTime();
        String coll = "c_chm_tomb";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedDoc(ctx, tenant, "chm.a", "paper", coll);
            seedDoc(ctx, tenant, "chm.b", "paper", coll);
            // chm.a has no inbound link (orphan); chm.b is the target of one (not orphan).
            ctx.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER,
                    CATALOG_LINKS.TO_TUMBLER, CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
                .values(tenant, "chm.a", "chm.b", "cites", "test")
                .execute();
        }
        assertThat(orphanCountFor(tenant, coll))
            .as("chm.a has no inbound link; chm.b does — one orphan").isEqualTo(1L);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("chm.a"))
                .execute();
        }
        assertThat(orphanCountFor(tenant, coll))
            .as("tombstoning the orphan removes it from scope — it must NOT still be "
                + "counted (the wrong-direction bug: deletion RAISING orphan_count)")
            .isEqualTo(0L);

        try (Connection su = pg.createConnection("")) {
            int count = DSL.using(su, SQLDialect.POSTGRES).selectCount().from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("chm.a"))
                .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())
                .fetchOne(0, int.class);
            assertThat(count).as("CONTROL: tombstoned row exists").isEqualTo(1);
        }
    }

    private static void seedOwner(DSLContext ctx, String tenant, String prefix) {
        ctx.insertInto(CATALOG_OWNERS, CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX,
                CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE)
            .values(tenant, prefix, prefix, "repo")
            .execute();
    }

    private static void seedColl(DSLContext ctx, String tenant, String name) {
        // Idempotent: seedTopic (RDR-164 P1a) may have already stub-registered this collection.
        PgContainerHelper.insertCollection(ctx, tenant, name);
    }

    private static void seedChunk(DSLContext ctx, String tenant, String docId, int pos, String chash,
                                   String collection) {
        // chash must be exactly 32 chars (catalog_document_chunks_chash_len_check),
        // stored as its own ASCII bytes -- matches the pre-conversion raw SQL's bare
        // string literal into a bytea column via Postgres's escape-format input.
        byte[] c = (chash + "00000000000000000000000000000000").substring(0, 32)
            .getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for every manifest write below.
        // RDR-191 Phase 5 (nexus-o8dil.49): nexus.chunks now ALSO carries
        // chunks_collection_fk (tenant_id, collection) -> catalog_collections
        // (tenant_id, name) — stub-register the collection first (idempotent,
        // mirrors seedColl above) rather than relying on every call site to have
        // already called it for this exact collection.
        seedColl(ctx, tenant, collection);
        float[] v = new float[384];
        java.util.Arrays.fill(v, 0.1f);
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384)
            .values(tenant, collection, c, "stub", Vector.of(v))
            .onConflictDoNothing()
            .execute();
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .values(tenant, docId, pos, c, collection)
            .execute();
    }

    // ── reloption physically set on every view ──────────────────────────────────

    @Test @Order(10)
    void everyView_hasSecurityInvokerReloption() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String view : VIEWS) {
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", view))
                    .as("view nexus.%s must exist", view).isTrue();
                String[] arr = PgCatalogProbes.relOptions(ctx, "nexus", view);
                assertThat(arr).as("nexus.%s must HAVE reloptions", view).isNotNull();
                assertThat(arr)
                    .as("nexus.%s must have security_invoker=true PHYSICALLY set (RDR-154 standing rule)", view)
                    .contains("security_invoker=true");
            }
        }
    }

    // ── grouped views leak zero foreign-tenant rows ─────────────────────────────

    @Test @Order(20)
    void groupedViews_gucA_seeZeroTenantBRows() throws Exception {
        // CONTROL: superuser sees BOTH tenants in each grouped view (foreign rows exist).
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String view : GROUPED_VIEWS) {
                Table<?> table = GROUPED_VIEW_TABLES.get(view);
                int count = ctx.selectCount().from(table)
                    .where(table.field("tenant_id", String.class).eq(TENANT_B))
                    .fetchOne(0, int.class);
                assertThat(count)
                    .as("CONTROL: superuser must see tenant-B rows in nexus.%s", view)
                    .isGreaterThanOrEqualTo(1);
            }
        }
        // svc + GUC=A: zero tenant-B rows; at least one tenant-A row.
        try (Connection svc = svcDs.getConnection()) {
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            for (String view : GROUPED_VIEWS) {
                Table<?> table = GROUPED_VIEW_TABLES.get(view);
                int countB = ctx.selectCount().from(table)
                    .where(table.field("tenant_id", String.class).eq(TENANT_B))
                    .fetchOne(0, int.class);
                assertThat(countB)
                    .as("GUC=A must see ZERO tenant-B rows in nexus.%s (caller RLS via security_invoker)", view)
                    .isEqualTo(0);
                int countA = ctx.selectCount().from(table)
                    .where(table.field("tenant_id", String.class).eq(TENANT_A))
                    .fetchOne(0, int.class);
                assertThat(countA)
                    .as("GUC=A must see its own tenant-A rows in nexus.%s", view)
                    .isGreaterThanOrEqualTo(1);
            }
        }
    }

    // ── scalar catalog_stats scopes counts to the GUC tenant ────────────────────

    @Test @Order(30)
    void catalogStats_scopesScalarCountsToGucTenant() throws Exception {
        try (Connection svc = svcDs.getConnection()) {
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_A, false);
            var a = ctx.select(CATALOG_STATS.DOC_COUNT, CATALOG_STATS.LINK_COUNT, CATALOG_STATS.OWNER_COUNT,
                    CATALOG_STATS.COLLECTION_COUNT, CATALOG_STATS.CHUNK_COUNT)
                .from(CATALOG_STATS)
                .fetchOne();
            assertThat(a.value1()).as("GUC=A doc_count").isEqualTo(2L);
            assertThat(a.value2()).as("GUC=A link_count").isEqualTo(1L);
            assertThat(a.value3()).as("GUC=A owner_count").isEqualTo(2L);
            assertThat(a.value4()).as("GUC=A collection_count").isEqualTo(2L);
            assertThat(a.value5()).as("GUC=A chunk_count").isEqualTo(2L);

            PgContainerHelper.setTenant(svc, TenantScope.DEFAULT_TENANT_GUC, TENANT_B, false);
            var b = ctx.select(CATALOG_STATS.DOC_COUNT, CATALOG_STATS.LINK_COUNT, CATALOG_STATS.OWNER_COUNT,
                    CATALOG_STATS.COLLECTION_COUNT, CATALOG_STATS.CHUNK_COUNT)
                .from(CATALOG_STATS)
                .fetchOne();
            assertThat(b.value1()).as("GUC=B doc_count").isEqualTo(1L);
            assertThat(b.value2()).as("GUC=B link_count").isEqualTo(2L);
            assertThat(b.value3()).as("GUC=B owner_count").isEqualTo(1L);
            assertThat(b.value4()).as("GUC=B collection_count").isEqualTo(1L);
            assertThat(b.value5()).as("GUC=B chunk_count").isEqualTo(1L);
        }
    }
}
