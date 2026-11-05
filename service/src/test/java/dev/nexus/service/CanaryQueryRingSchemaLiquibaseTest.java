// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-4lnn1.1.2 (RDR-230 Phase 1 P1.2a): {@code vectors-054-canary-query-ring.xml} adds
 * {@code nexus.canary_query_ring} (the recall canary's persisted query ring) and
 * {@code nexus.canary_query_ring_upsert}, the batch upsert-and-trim function.
 *
 * <p>Three shapes. A walk on a dedicated database (migrate to just before the changeset, assert the objects are
 * absent, apply, roll the two changesets back, reapply). The table's shape on a migrated database (columns, keys,
 * CHECKs, the cascading foreign key, FORCE row-level security) as the superuser, and its CHECKs and cascade
 * enforced. And the function as the NOBYPASSRLS service role under {@link TenantScope}, so row-level security
 * applies: a batch whose entry names a deleted collection (gate S6) writes the others, the 32-newest and 30-day
 * bounds, a refresh in place, dimension routing, and tenant isolation.
 *
 * <p>The moves of a ring with its collection (rename, re-home, ghost sweep) are in
 * {@link CanaryQueryRingCollectionMovesTest} and {@code GhostSweepDormantMarkingTest}; the nexus_diag denial is in
 * {@link GrantsNexusDiagViewAccessIntegrationTest}. No statement is written as a string here: the schema probes are
 * {@link PgCatalogProbes}, the rows are typed jOOQ, the function is the generated routine.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CanaryQueryRingSchemaLiquibaseTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    /** The last changeset of the changelog before vectors-054 (catalog-047 is the tail it was placed after). */
    private static final String PRIOR_CHANGESET_ID = "catalog-047-3";
    private static final String LAST_TARGET_CHANGESET_ID = "vectors-054-2";

    private static final String SVC_ROLE = "svc_ring054_test";
    private static final String SVC_PASS = "svc_ring054_pass";

    /** Registered under the model the name carries, 384 wide. */
    private static final String COLL_384 = "knowledge__ring054-a__minilm-l6-v2-384__v1";
    private static final String COLL_384_OTHER = "knowledge__ring054-b__minilm-l6-v2-384__v1";
    /** A collection whose registry row is NEVER created, so an entry naming it is the S6 case. */
    private static final String COLL_GONE = "knowledge__ring054-gone__minilm-l6-v2-384__v1";

    /** column -> information_schema udt_name + is_nullable. */
    private static final Map<String, String> EXPECTED_COLUMNS = new TreeMap<>(Map.ofEntries(
        Map.entry("tenant_id", "text NO"),
        Map.entry("collection", "text NO"),
        Map.entry("embedding_model", "text NO"),
        Map.entry("embedding_384", "vector YES"),
        Map.entry("embedding_768", "vector YES"),
        Map.entry("embedding_1024", "vector YES"),
        Map.entry("vector_digest", "bytea NO"),
        Map.entry("added_at", "timestamptz NO")));

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope scope;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── the walk ────────────────────────────────────────────────────────

    @Test
    void addsTheTableAndTheFunction_rollsBackAndReapplies() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, PRIOR_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "canary_query_ring"))
                    .as("before the changeset").isFalse();
                assertThat(PgCatalogProbes.routineExists(ctx, "nexus", "canary_query_ring_upsert")).isFalse();
            }
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, LAST_TARGET_CHANGESET_ID);
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "canary_query_ring")).isTrue();
                assertThat(PgCatalogProbes.routineExists(ctx, "nexus", "canary_query_ring_upsert")).isTrue();
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                try (Liquibase liquibase = new Liquibase(
                        MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                    liquibase.rollback(2, new Contexts(), new LabelExpression());
                }
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "canary_query_ring"))
                    .as("rollback drops the table").isFalse();
                assertThat(PgCatalogProbes.routineExists(ctx, "nexus", "canary_query_ring_upsert"))
                    .as("rollback drops the function").isFalse();
            }
            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
                liquibase.update(new Contexts(), new LabelExpression());
                su.setAutoCommit(true);
            }
            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "canary_query_ring")).isTrue();
                assertThat(PgCatalogProbes.routineExists(ctx, "nexus", "canary_query_ring_upsert")).isTrue();
            }
        } finally {
            dedicated.stop();
        }
    }

    // ── the table's shape ───────────────────────────────────────────────

    @Test
    void ring_hasExactColumnsKeysChecksAndACascadingForeignKey() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            Map<String, String> actual = new TreeMap<>();
            for (String col : PgCatalogProbes.columnNames(ctx, "nexus", "canary_query_ring")) {
                PgCatalogProbes.ColumnInfo info = PgCatalogProbes.columnInfo(ctx, "nexus", "canary_query_ring", col);
                actual.put(col, info.udtName() + " " + info.isNullable());
            }
            assertThat(actual).as("nexus.canary_query_ring exact column set, types and nullability")
                .isEqualTo(EXPECTED_COLUMNS);

            Map<String, String> constraints = PgCatalogProbes.constraintDefinitionsIn(ctx, List.of("nexus")).stream()
                .filter(c -> c.table().equals("canary_query_ring"))
                .collect(Collectors.toMap(
                    PgCatalogProbes.ConstraintDefinition::conname,
                    PgCatalogProbes.ConstraintDefinition::definition));
            assertThat(constraints).containsOnlyKeys(
                "canary_query_ring_pk", "canary_query_ring_exactly_one_embedding", "canary_query_ring_digest_len",
                "canary_query_ring_collection_fk");
            assertThat(constraints.get("canary_query_ring_pk"))
                .isEqualTo("PRIMARY KEY (tenant_id, collection, vector_digest)");
            assertThat(constraints.get("canary_query_ring_exactly_one_embedding"))
                .contains("num_nonnulls(embedding_384, embedding_768, embedding_1024) = 1");
            assertThat(constraints.get("canary_query_ring_digest_len")).contains("octet_length(vector_digest) = 32");
            assertThat(constraints.get("canary_query_ring_collection_fk"))
                .contains("FOREIGN KEY (tenant_id, collection, embedding_model)")
                .contains("catalog_collections(tenant_id, name, embedding_model)")
                .contains("ON DELETE CASCADE");

            assertThat(PgCatalogProbes.foreignKeyDeleteActions(ctx, "nexus", "canary_query_ring"))
                .as("the one foreign key cascades on delete ('c')").containsExactly("c");
            assertThat(PgCatalogProbes.indexDefs(ctx, "nexus", "canary_query_ring"))
                .as("no index but the primary key's: nothing on the vectors")
                .hasSize(1).allMatch(d -> d.contains("canary_query_ring_pk"));
        }
    }

    @Test
    void ring_rlsEnabledForcedWithTenantIsolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "canary_query_ring");
            assertThat(rls).as("nexus.canary_query_ring must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("RLS ENABLE").isTrue();
            assertThat(rls.forced()).as("RLS FORCE").isTrue();
            List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "canary_query_ring");
            assertThat(policies).hasSize(1);
            assertThat(policies.get(0).policyname()).isEqualTo("tenant_isolation");
            assertThat(policies.get(0).qual()).contains("nexus.tenant");
            assertThat(policies.get(0).withCheck()).contains("nexus.tenant");
        }
    }

    @Test
    void theUpsertFunction_isInvokerWithAPinnedSearchPath_andServiceCanExecuteIt() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.routineSecurityDefiner(ctx, "nexus", "canary_query_ring_upsert"))
                .as("SECURITY INVOKER, so row-level security applies to the caller").isFalse();
            assertThat(PgCatalogProbes.routineConfigAndSource(ctx, "nexus", "canary_query_ring_upsert").proconfig())
                .contains("search_path=pg_catalog, pg_temp");
            assertThat(PgCatalogProbes.hasExplicitRoutineGrant(ctx, "nexus", "canary_query_ring_upsert",
                "nexus_svc", "EXECUTE"))
                .as("the GRANT EXECUTE to nexus_svc was issued, not inherited from PUBLIC").isTrue();
        }
    }

    // ── the CHECKs and the cascade, enforced ────────────────────────────

    @Test
    void checks_refuseAWrongDimensionTwoColumnsNoColumnAndAShortDigest() throws Exception {
        String tenant = "ring054-checks";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, COLL_384);
            String model = PgContainerHelper.collectionModel(ctx, tenant, COLL_384);
            OffsetDateTime now = OffsetDateTime.now();

            assertThatThrownBy(() -> ctx.insertInto(CANARY_QUERY_RING)
                .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, COLL_384)
                .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
                .set(CANARY_QUERY_RING.EMBEDDING_384, vector(768, 1))
                .set(CANARY_QUERY_RING.VECTOR_DIGEST, digest("wrong-dim")).set(CANARY_QUERY_RING.ADDED_AT, now)
                .execute())
                .as("a 768-wide vector in the 384-wide column: the typed column refuses it")
                .isInstanceOf(DataAccessException.class)
                .hasMessageContaining("expected 384 dimensions, not 768");

            assertThatThrownBy(() -> ctx.insertInto(CANARY_QUERY_RING)
                .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, COLL_384)
                .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
                .set(CANARY_QUERY_RING.EMBEDDING_384, vector(384, 1))
                .set(CANARY_QUERY_RING.EMBEDDING_768, vector(768, 1))
                .set(CANARY_QUERY_RING.VECTOR_DIGEST, digest("two")).set(CANARY_QUERY_RING.ADDED_AT, now)
                .execute())
                .as("two embedding columns set")
                .hasMessageContaining("canary_query_ring_exactly_one_embedding");

            assertThatThrownBy(() -> ctx.insertInto(CANARY_QUERY_RING)
                .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, COLL_384)
                .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
                .set(CANARY_QUERY_RING.VECTOR_DIGEST, digest("none")).set(CANARY_QUERY_RING.ADDED_AT, now)
                .execute())
                .as("no embedding column set")
                .hasMessageContaining("canary_query_ring_exactly_one_embedding");

            assertThatThrownBy(() -> ctx.insertInto(CANARY_QUERY_RING)
                .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, COLL_384)
                .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
                .set(CANARY_QUERY_RING.EMBEDDING_384, vector(384, 1))
                .set(CANARY_QUERY_RING.VECTOR_DIGEST, new byte[31]).set(CANARY_QUERY_RING.ADDED_AT, now)
                .execute())
                .as("a digest that is not 32 bytes")
                .hasMessageContaining("canary_query_ring_digest_len");

            assertThatThrownBy(() -> ctx.insertInto(CANARY_QUERY_RING)
                .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, COLL_GONE)
                .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
                .set(CANARY_QUERY_RING.EMBEDDING_384, vector(384, 1))
                .set(CANARY_QUERY_RING.VECTOR_DIGEST, digest("orphan")).set(CANARY_QUERY_RING.ADDED_AT, now)
                .execute())
                .as("a ring row naming a collection with no registry row")
                .hasMessageContaining("canary_query_ring_collection_fk");

            assertThat(ctx.fetchCount(CANARY_QUERY_RING, CANARY_QUERY_RING.TENANT_ID.eq(tenant)))
                .as("none of the refused rows landed").isZero();
        }
    }

    @Test
    void deletingTheRegistryRow_deletesTheRingRows_andOnlyThatCollectionsRows() throws Exception {
        String tenant = "ring054-cascade";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, tenant, COLL_384);
            PgContainerHelper.insertCollection(ctx, tenant, COLL_384_OTHER);
            String model = PgContainerHelper.collectionModel(ctx, tenant, COLL_384);
            for (int i = 0; i < 3; i++) {
                insertRingRow(ctx, tenant, COLL_384, model, "cascade-a-" + i, OffsetDateTime.now());
            }
            insertRingRow(ctx, tenant, COLL_384_OTHER, model, "cascade-b-0", OffsetDateTime.now());
            assertThat(ctx.fetchCount(CANARY_QUERY_RING, CANARY_QUERY_RING.TENANT_ID.eq(tenant))).isEqualTo(4);

            int deleted = ctx.deleteFrom(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(COLL_384))
                .execute();

            assertThat(deleted).as("the registry row is deletable: the ring does not restrict it").isEqualTo(1);
            assertThat(ctx.fetchCount(CANARY_QUERY_RING,
                CANARY_QUERY_RING.TENANT_ID.eq(tenant).and(CANARY_QUERY_RING.COLLECTION.eq(COLL_384))))
                .as("the deleted collection's ring went with it (ON DELETE CASCADE)").isZero();
            assertThat(ctx.fetchCount(CANARY_QUERY_RING,
                CANARY_QUERY_RING.TENANT_ID.eq(tenant).and(CANARY_QUERY_RING.COLLECTION.eq(COLL_384_OTHER))))
                .as("a sibling collection's ring is untouched").isEqualTo(1);
        }
    }

    // ── the function ────────────────────────────────────────────────────

    @Test
    void upsert_insertsEntries_andRefreshesAnExistingDigestInPlace() throws Exception {
        String tenant = "ring054-refresh";
        seedCollection(tenant, COLL_384);

        int written = upsert(tenant, entry(COLL_384, "r1", 384), entry(COLL_384, "r2", 384));
        assertThat(written).as("two entries inserted").isEqualTo(2);
        List<RingRow> first = ring(tenant, COLL_384);
        assertThat(first).extracting(RingRow::digestHex)
            .containsExactlyInAnyOrder(hex(digest("r1")), hex(digest("r2")));

        // r1 again: the same digest in the same ring is a refresh of added_at, not a second row
        assertThat(upsert(tenant, entry(COLL_384, "r1", 384))).isEqualTo(1);

        List<RingRow> second = ring(tenant, COLL_384);
        assertThat(second).as("still two rows").hasSize(2);
        assertThat(second.get(0).digestHex()).as("r1 is now the newest").isEqualTo(hex(digest("r1")));
        OffsetDateTime r1Before = first.stream().filter(r -> r.digestHex().equals(hex(digest("r1"))))
            .findFirst().orElseThrow().addedAt();
        assertThat(second.get(0).addedAt()).as("added_at moved forward").isAfter(r1Before);
    }

    @Test
    void upsert_anEntryWhoseCollectionWasDeleted_isSkipped_andTheRestOfTheBatchIsWritten() throws Exception {
        // Gate S6: the collection was deleted between enqueue and flush. The function skips the entry on its
        // foreign-key failure (a sub-block per entry), so the batch's other entries, before AND after it, land.
        String tenant = "ring054-s6";
        seedCollection(tenant, COLL_384);
        seedCollection(tenant, COLL_384_OTHER);

        int written = upsert(tenant,
            entry(COLL_384, "s6-before", 384),
            entry(COLL_GONE, "s6-gone", 384),
            entry(COLL_384_OTHER, "s6-after", 384),
            entry(COLL_384, "s6-last", 384));

        assertThat(written).as("three of four written: the shortfall is the skipped entry").isEqualTo(3);
        assertThat(ring(tenant, COLL_384)).extracting(RingRow::digestHex)
            .containsExactlyInAnyOrder(hex(digest("s6-before")), hex(digest("s6-last")));
        assertThat(ring(tenant, COLL_384_OTHER)).extracting(RingRow::digestHex)
            .containsExactly(hex(digest("s6-after")));
        assertThat(ring(tenant, COLL_GONE)).as("nothing was written for the deleted collection").isEmpty();
    }

    @Test
    void upsert_anEntryNamingTheWrongModelForARegisteredCollection_isSkippedToo() throws Exception {
        String tenant = "ring054-s6-model";
        seedCollection(tenant, COLL_384);

        int written = upsert(tenant,
            entry(COLL_384, "model-ok", 384),
            new Entry(COLL_384, "a-model-the-registry-does-not-hold", "model-bad", 384));

        assertThat(written).isEqualTo(1);
        assertThat(ring(tenant, COLL_384)).extracting(RingRow::digestHex)
            .containsExactly(hex(digest("model-ok")));
    }

    @Test
    void upsert_keepsTheThirtyTwoNewestRowsOfARing_andLeavesAnotherRingAlone() throws Exception {
        String tenant = "ring054-bound";
        seedCollection(tenant, COLL_384);
        seedCollection(tenant, COLL_384_OTHER);
        // The other ring holds 40 rows written by hand (the function would have trimmed them): the trim of
        // COLL_384 must not touch it.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(ctx, tenant, COLL_384_OTHER);
            for (int i = 0; i < 40; i++) {
                insertRingRow(ctx, tenant, COLL_384_OTHER, model, "other-" + i, OffsetDateTime.now().minusMinutes(i));
            }
        }

        Entry[] batch = new Entry[40];
        for (int i = 0; i < 40; i++) batch[i] = entry(COLL_384, "bound-" + i, 384);
        assertThat(upsert(tenant, batch)).as("all forty entries are written, then the ring is trimmed").isEqualTo(40);

        List<RingRow> ring = ring(tenant, COLL_384);
        assertThat(ring).as("a ring holds at most 32 rows").hasSize(32);
        List<String> expected = new ArrayList<>();
        for (int i = 39; i >= 8; i--) expected.add(hex(digest("bound-" + i)));
        assertThat(ring).extracting(RingRow::digestHex)
            .as("the 32 newest by added_at, newest first: entries 39 down to 8").containsExactlyElementsOf(expected);
        assertThat(ring(tenant, COLL_384_OTHER)).as("a ring this call did not write is not trimmed").hasSize(40);

        // And the next write evicts the oldest of the 32.
        assertThat(upsert(tenant, entry(COLL_384, "bound-40", 384))).isEqualTo(1);
        List<RingRow> after = ring(tenant, COLL_384);
        assertThat(after).hasSize(32);
        assertThat(after.get(0).digestHex()).isEqualTo(hex(digest("bound-40")));
        assertThat(after).extracting(RingRow::digestHex).doesNotContain(hex(digest("bound-8")));
    }

    @Test
    void upsert_dropsRowsOlderThanThirtyDays_ofTheRingItWrote_butKeepsYoungerOnes() throws Exception {
        String tenant = "ring054-age";
        seedCollection(tenant, COLL_384);
        seedCollection(tenant, COLL_384_OTHER);
        OffsetDateTime now = OffsetDateTime.now();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(ctx, tenant, COLL_384);
            insertRingRow(ctx, tenant, COLL_384, model, "age-31d", now.minusDays(31));
            insertRingRow(ctx, tenant, COLL_384, model, "age-29d", now.minusDays(29));
            // an old row in a ring the call does not write: left for the pass-time delete of the next bead
            insertRingRow(ctx, tenant, COLL_384_OTHER, model, "age-other-31d", now.minusDays(31));
        }

        assertThat(upsert(tenant, entry(COLL_384, "age-new", 384))).isEqualTo(1);

        assertThat(ring(tenant, COLL_384)).extracting(RingRow::digestHex)
            .as("the 31-day row is gone, the 29-day row and the new one stay")
            .containsExactlyInAnyOrder(hex(digest("age-29d")), hex(digest("age-new")));
        assertThat(ring(tenant, COLL_384_OTHER)).extracting(RingRow::digestHex)
            .as("a ring the call did not write is not aged").containsExactly(hex(digest("age-other-31d")));
    }

    @Test
    void upsert_routesEachVectorToTheColumnOfItsDimension_andSkipsAnUnsupportedWidth() throws Exception {
        String tenant = "ring054-dims";
        String coll768 = "knowledge__ring054-768__bge-base-en-v15-768__v1";
        String coll1024 = "knowledge__ring054-1024__voyage-context-3__v1";
        seedCollection(tenant, coll768);
        seedCollection(tenant, coll1024);
        seedCollection(tenant, COLL_384);

        int written = upsert(tenant,
            entry(COLL_384, "dim-384", 384),
            entry(coll768, "dim-768", 768),
            entry(coll1024, "dim-1024", 1024),
            entry(COLL_384, "dim-10", 10));

        assertThat(written).as("the 10-wide vector fits no column and is skipped").isEqualTo(3);
        Map<String, List<Boolean>> columnsSet = scope.withTenant(tenant, ctx -> ctx
            .select(CANARY_QUERY_RING.COLLECTION, DSL.field(CANARY_QUERY_RING.EMBEDDING_384.isNotNull()),
                    DSL.field(CANARY_QUERY_RING.EMBEDDING_768.isNotNull()),
                    DSL.field(CANARY_QUERY_RING.EMBEDDING_1024.isNotNull()))
            .from(CANARY_QUERY_RING).fetch().stream().collect(Collectors.toMap(
                r -> r.value1(), r -> List.of(r.value2(), r.value3(), r.value4()))));
        assertThat(columnsSet).containsEntry(COLL_384, List.of(true, false, false))
            .containsEntry(coll768, List.of(false, true, false))
            .containsEntry(coll1024, List.of(false, false, true))
            .hasSize(3);
    }

    @Test
    void upsert_isTenantScoped_aCallUnderAnotherTenantIsRefused_andReadsDoNotCross() throws Exception {
        String a = "ring054-iso-a";
        String b = "ring054-iso-b";
        seedCollection(a, COLL_384);
        seedCollection(b, COLL_384);
        assertThat(upsert(a, entry(COLL_384, "iso", 384))).isEqualTo(1);

        assertThat(ring(b, COLL_384)).as("tenant B reads none of tenant A's ring").isEmpty();
        assertThatThrownBy(() -> scope.withTenant(b, ctx -> Routines.canaryQueryRingUpsert(ctx.configuration(),
                a, new String[] {COLL_384}, new String[] {modelOf(a, COLL_384)},
                new String[] {vector(384, 7).toString()}, new byte[][] {digest("iso-cross")})))
            .as("a session of tenant B naming tenant A: the WITH CHECK refuses the row")
            .hasMessageContaining("row-level security");
        assertThat(ring(a, COLL_384)).as("and tenant A's ring is unchanged").hasSize(1);
    }

    @Test
    void upsert_refusesArraysOfDifferentLengths_andAnEmptyBatchWritesNothing() throws Exception {
        String tenant = "ring054-args";
        seedCollection(tenant, COLL_384);

        assertThatThrownBy(() -> scope.withTenant(tenant, ctx -> Routines.canaryQueryRingUpsert(ctx.configuration(),
                tenant, new String[] {COLL_384, COLL_384}, new String[] {modelOf(tenant, COLL_384)},
                new String[] {vector(384, 1).toString()}, new byte[][] {digest("len")})))
            .hasMessageContaining("the four arrays must have the same length");
        assertThat(upsert(tenant)).as("an empty batch").isZero();
        assertThat(ring(tenant, COLL_384)).isEmpty();
    }

    // ── fixtures ────────────────────────────────────────────────────────

    /** One batch entry: the collection, the model the registry holds for it (looked up), the seed, the width. */
    private record Entry(String collection, String model, String seed, int dims) {}

    private record RingRow(String digestHex, OffsetDateTime addedAt) {}

    /** An entry for {@code collection} under the model its registry row holds ("" until {@link #upsert} fills it). */
    private static Entry entry(String collection, String seed, int dims) {
        return new Entry(collection, null, seed, dims);
    }

    private void seedCollection(String tenant, String name) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, name);
        }
    }

    private String modelOf(String tenant, String collection) {
        return scope.withTenant(tenant, ctx -> PgContainerHelper.collectionModel(ctx, tenant, collection));
    }

    /** One call of the generated routine; an entry with no model takes its collection's, or "none" when unregistered. */
    private int upsert(String tenant, Entry... entries) {
        String[] collections = new String[entries.length];
        String[] models = new String[entries.length];
        String[] vectors = new String[entries.length];
        byte[][] digests = new byte[entries.length][];
        for (int i = 0; i < entries.length; i++) {
            Entry e = entries[i];
            collections[i] = e.collection();
            models[i] = e.model() != null ? e.model() : registeredModelOrDefault(tenant, e.collection());
            vectors[i] = vector(e.dims(), i + 1).toString();
            digests[i] = digest(e.seed());
        }
        Integer written = scope.withTenant(tenant, ctx -> Routines.canaryQueryRingUpsert(
            ctx.configuration(), tenant, collections, models, vectors, digests));
        return written;
    }

    /**
     * The model the registry holds for the entry's collection; for one with no registry row (the S6 case) the model
     * a registered sibling of the same name pattern would carry, so that the only thing wrong with the entry is
     * that its collection is gone.
     */
    private String registeredModelOrDefault(String tenant, String collection) {
        String model = scope.withTenant(tenant, ctx -> ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
            .fetchOne(CATALOG_COLLECTIONS.EMBEDDING_MODEL));
        return model != null ? model : modelOf(tenant, COLL_384);
    }

    /** The ring's rows as the tenant sees them, newest first. */
    private List<RingRow> ring(String tenant, String collection) {
        return scope.withTenant(tenant, ctx -> ctx
            .select(CANARY_QUERY_RING.VECTOR_DIGEST, CANARY_QUERY_RING.ADDED_AT)
            .from(CANARY_QUERY_RING)
            .where(CANARY_QUERY_RING.COLLECTION.eq(collection))
            .orderBy(CANARY_QUERY_RING.ADDED_AT.desc(), CANARY_QUERY_RING.VECTOR_DIGEST)
            .fetch(r -> new RingRow(hex(r.value1()), r.value2())));
    }

    private static void insertRingRow(DSLContext ctx, String tenant, String collection, String model, String seed,
                                      OffsetDateTime addedAt) {
        ctx.insertInto(CANARY_QUERY_RING)
            .set(CANARY_QUERY_RING.TENANT_ID, tenant).set(CANARY_QUERY_RING.COLLECTION, collection)
            .set(CANARY_QUERY_RING.EMBEDDING_MODEL, model)
            .set(CANARY_QUERY_RING.EMBEDDING_384, vector(384, seed.hashCode() & 0xff))
            .set(CANARY_QUERY_RING.VECTOR_DIGEST, digest(seed)).set(CANARY_QUERY_RING.ADDED_AT, addedAt)
            .execute();
    }

    private static Vector vector(int dims, int seed) {
        float[] v = new float[dims];
        for (int i = 0; i < dims; i++) v[i] = ((seed * 31 + i) % 97) / 100f + 0.01f;
        return Vector.of(v);
    }

    /** SHA-256 of the seed: 32 bytes, as the digest of a query vector is. */
    private static byte[] digest(String seed) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(seed.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String hex(byte[] bytes) {
        return HexFormat.of().formatHex(bytes);
    }

    // ── the walk helper ─────────────────────────────────────────────────

    private static void migrateUpTo(Connection conn, String targetChangesetId) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
        int idx = -1;
        for (int i = 0; i < unrun.size(); i++) {
            if (targetChangesetId.equals(unrun.get(i).getId())) {
                idx = i;
                break;
            }
        }
        assertThat(idx).as(targetChangesetId + " must be present in the master changelog").isGreaterThanOrEqualTo(0);
        liquibase.update(idx + 1, new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }
}
