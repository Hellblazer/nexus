// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.test.Routines;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Collectors;

import static dev.nexus.service.PartitionScratch.BGE_768;
import static dev.nexus.service.PartitionScratch.CODE_3;
import static dev.nexus.service.PartitionScratch.CONTEXT_3;
import static dev.nexus.service.PartitionScratch.MINILM_384;
import static dev.nexus.service.PartitionScratch.expectedName;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static dev.nexus.service.jooq.nexus.Tables.GC_AUDIT;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 P1.3 (nexus-3wh8d.8): the single migration changeset {@code vectors-030-1} (steps 2 to 7.8), run as
 * production runs it: a NOSUPERUSER, NOBYPASSRLS schema-owner role migrates a store that was walked up to, but
 * not including, the changeset and then seeded through the pre-walk layout.
 *
 * <p>The seed is two tenants with a token, one tenant with chunks and no token, two real models per tenant,
 * hidden rows (chunks no manifest row claims), a centroid whose collection is unregistered, and referencing
 * rows on all three referencing tables. Legacy data shapes (a vector whose dimension disagrees with its
 * collection's model) are not supported: the walk fails on one, and a test below pins that.
 * {@link #EXPECTED_CHUNKS} is the oracle, written down by hand from the rules, not read back from the migrated
 * store.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class P225MigrationWalkIntegrationTest {

    private static final String ADMIN = "nexus_admin_p225walk";
    private static final String ADMIN_PASS = "nexus_admin_p225walk_pass";
    private static final String MASTER = "db/changelog/db.changelog-master.xml";
    private static final String WALK = "vectors-030-1";

    // tenants
    static final String TA = "p225-a";
    static final String TB = "p225-b";
    static final String TN = "p225-notoken";

    // collections (the model token in the name is the registered model)
    static final String A_CODE = "code__a-owner__voyage-code-3__v1";
    static final String A_CTX = "docs__a-owner__voyage-context-3__v1";
    static final String A_MINI = "knowledge__a-owner__minilm-l6-v2-384__v1";
    static final String B_CODE = "code__b-owner__voyage-code-3__v1";
    static final String B_BGE = "docs__b-owner__bge-base-en-v15-768__v1";
    static final String N_CODE = "code__nt-owner__voyage-code-3__v1";
    static final String GHOST = "ghost-collection-without-a-registry-row";

    /** (model, tenant) -> chunk count after the walk, by the rules, from the seed. */
    static final Map<String, Long> EXPECTED_CHUNKS = new TreeMap<>(Map.of(
        CODE_3 + "|" + TA, 3L,        // A_CODE (one hidden)
        CODE_3 + "|" + TB, 2L,
        CODE_3 + "|" + TN, 1L,        // a tenant with chunks and no token row
        CONTEXT_3 + "|" + TA, 2L,
        MINILM_384 + "|" + TA, 2L,    // both hidden
        BGE_768 + "|" + TB, 2L));

    /** (model, tenant) -> centroid count after the walk. */
    static final Map<String, Long> EXPECTED_CENTROIDS = new TreeMap<>(Map.of(
        CODE_3 + "|" + TA, 2L,
        MINILM_384 + "|" + TA, 1L,
        CODE_3 + "|" + TB, 1L));

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;

    /** Pre-walk facts the post-walk assertions compare against. */
    List<PartitionScratch.PolicyRow> preWalkChunksPolicies;
    TreeSet<String> preWalkChunksAcl;

    @BeforeAll
    void walkSeededStore() throws Exception {
        pg = PgContainerHelper.startDedicated();
        Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
        adminDs = pool(pg, 4);
        migrateUpTo(adminDs, WALK);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            seedStore(ctx);
            preWalkChunksPolicies = PartitionScratch.policies(ctx, "chunks");
            preWalkChunksAcl = PartitionScratch.acl(ctx, "chunks");
        }
        applyRemaining(adminDs);
        try (Connection a = adminDs.getConnection()) {
            PgContainerHelper.installTestObjects(a);
        }
    }

    @AfterAll
    void stop() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    // ═══════════════════════════ the walk's result ═══════════════════════════

    @Test
    @Order(1)
    void chunkCountsReconcileExactlyPerModelAndTenant_hiddenRowsIncluded() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Map<String, Long> actual = new TreeMap<>();
            ctx.select(CHUNKS.EMBEDDING_MODEL, CHUNKS.TENANT_ID, DSL.count())
                .from(CHUNKS).groupBy(CHUNKS.EMBEDDING_MODEL, CHUNKS.TENANT_ID)
                .forEach(r -> actual.put(r.value1() + "|" + r.value2(), r.value3().longValue()));
            assertThat(actual).isEqualTo(EXPECTED_CHUNKS);
            // The hidden rows (no manifest row claims them) are present, not dropped.
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(A_MINI))).isEqualTo(2);
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(N_CODE))).isEqualTo(1);
            // And the old table still holds exactly what it held (a recovery copy).
            assertThat(retiredCount(ctx, "chunks_retired_225")).isEqualTo(12L);
        }
    }

    @Test
    @Order(2)
    void everyChunkLiesInTheLeafItsModelAndTenantNameAndCarriesItsCollectionsModel() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Field<String> leaf = tableoid();
            var rows = ctx.select(CHUNKS.TENANT_ID, CHUNKS.EMBEDDING_MODEL, leaf, DSL.count())
                .from(CHUNKS).groupBy(CHUNKS.TENANT_ID, CHUNKS.EMBEDDING_MODEL, leaf).fetch();
            assertThat(rows).isNotEmpty();
            for (var r : rows) {
                assertThat(r.value3()).as("leaf of %s/%s", r.value1(), r.value2())
                    .endsWith(expectedName("chunks", r.value2(), r.value1()));
            }
            // A chunk's model is its collection's model, everywhere.
            assertThat(ctx.fetchCount(CHUNKS.join(CATALOG_COLLECTIONS)
                .on(CATALOG_COLLECTIONS.TENANT_ID.eq(CHUNKS.TENANT_ID)).and(CATALOG_COLLECTIONS.NAME.eq(CHUNKS.COLLECTION)),
                CATALOG_COLLECTIONS.EMBEDDING_MODEL.ne(CHUNKS.EMBEDDING_MODEL))).isZero();
            // The dimension CHECK holds by construction: only the model's own vector column is populated.
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.EMBEDDING_MODEL.in(CODE_3, CONTEXT_3), CHUNKS.EMBEDDING_1024.isNull())).isZero();
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.EMBEDDING_MODEL.eq(BGE_768), CHUNKS.EMBEDDING_768.isNull())).isZero();
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.EMBEDDING_MODEL.eq(MINILM_384), CHUNKS.EMBEDDING_384.isNull())).isZero();
        }
    }

    @Test
    @Order(3)
    void centroidsFollowTheirCollectionsModel_andTheOnesWithNoRegistryRowAreNotCopied() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Map<String, Long> actual = new TreeMap<>();
            ctx.select(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.TENANT_ID, DSL.count())
                .from(TAXONOMY_CENTROIDS).groupBy(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, TAXONOMY_CENTROIDS.TENANT_ID)
                .forEach(r -> actual.put(r.value1() + "|" + r.value2(), r.value3().longValue()));
            assertThat(actual).isEqualTo(EXPECTED_CENTROIDS);
            // 5 centroids seeded; 1 is not copied (its collection has no registry row: derived data) and stays put.
            assertThat(retiredCount(ctx, "taxonomy_centroids_retired_225")).isEqualTo(5L);
            Field<String> leaf = tableoid();
            for (var r : ctx.select(TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.EMBEDDING_MODEL, leaf)
                    .from(TAXONOMY_CENTROIDS).fetch()) {
                assertThat(r.value3()).endsWith(expectedName("taxonomy_centroids", r.value2(), r.value1()));
            }
        }
    }

    @Test
    @Order(5)
    void everyTenantSeenAnywhereHasALeafUnderEveryModelPartition_onBothParents() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String> models = List.of(CODE_3, CONTEXT_3, BGE_768, MINILM_384);
            List<String> tenants = List.of("default", TA, TB, TN);   // default, tokens, chunks-only
            for (String parent : List.of("chunks", "taxonomy_centroids")) {
                List<String> modelParts = PartitionScratch.children(ctx, parent).stream().map(PartitionScratch.Child::name).toList();
                assertThat(modelParts).containsExactlyInAnyOrderElementsOf(
                    models.stream().map(m -> expectedName(parent, m, null)).toList());
                for (String m : models) {
                    List<String> leaves = PartitionScratch.children(ctx, expectedName(parent, m, null)).stream()
                        .map(PartitionScratch.Child::name).toList();
                    assertThat(leaves).as("%s/%s", parent, m).containsExactlyInAnyOrderElementsOf(
                        tenants.stream().map(t -> expectedName(parent, m, t)).toList());
                }
            }
        }
    }

    @Test
    @Order(6)
    void forceRlsAndPoliciesAndGrantsHoldOnEveryParentModelPartitionAndLeaf() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String parent : List.of("chunks", "taxonomy_centroids")) {
                var policiesOfParent = PartitionScratch.policies(ctx, parent);
                var aclOfParent = PartitionScratch.acl(ctx, parent);
                List<String> all = new ArrayList<>(List.of(parent));
                for (var mp : PartitionScratch.children(ctx, parent)) {
                    all.add(mp.name());
                    PartitionScratch.children(ctx, mp.name()).forEach(l -> all.add(l.name()));
                }
                assertThat(all).hasSize(1 + 4 + 4 * 4);
                for (String rel : all) {
                    var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", rel);
                    assertThat(rls.enabled()).as("RLS enabled on %s", rel).isTrue();
                    assertThat(rls.forced()).as("RLS forced on %s", rel).isTrue();
                    assertThat(PartitionScratch.policies(ctx, rel)).as("policies of %s", rel).isEqualTo(policiesOfParent);
                    assertThat(PartitionScratch.acl(ctx, rel)).as("grants of %s", rel).isEqualTo(aclOfParent);
                }
            }
            // The live parent carries the policies the retired table carried, byte for byte.
            assertThat(PartitionScratch.policies(ctx, "chunks")).isEqualTo(preWalkChunksPolicies);
            assertThat(PartitionScratch.policies(ctx, "chunks")).extracting(PartitionScratch.PolicyRow::name)
                .contains("tenant_isolation", "chunks_gate_probe_owner_read");
            // Every grant the old table carried reached the new parent (the runAlways grants then added to both).
            assertThat(PartitionScratch.acl(ctx, "chunks")).containsAll(preWalkChunksAcl);
            // FORCE is back on every table the walk toggled, the retired ones included.
            for (String t : List.of("chunks_retired_225", "taxonomy_centroids_retired_225", "catalog_collections",
                    "catalog_document_chunks", "topic_assignments", "chunk_orphaned_at", "gc_audit")) {
                assertThat(PgCatalogProbes.rowSecurity(ctx, "nexus", t).forced()).as("FORCE on %s", t).isTrue();
            }
        }
    }

    @Test
    @Order(7)
    void theThreeViewsAreOverTheNewTables_withTheirOptionsAndGrants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String v : List.of("live_chunks", "collection_vector_stats", "diag_chash_conformance")) {
                assertThat(PgCatalogProbes.viewExists(ctx, "nexus", v)).as(v).isTrue();
                assertThat(PgCatalogProbes.relOptions(ctx, "nexus", v)).as("options of %s", v).contains("security_invoker=true");
                assertThat(viewDependsOn(ctx, v, "chunks")).as("%s reads the NEW chunks", v).isTrue();
                assertThat(viewDependsOn(ctx, v, "chunks_retired_225")).as("%s must not read the retired table", v).isFalse();
            }
            // The PUBLIC read grant vectors-019 put on two of them came back with the recreated views.
            assertThat(PartitionScratch.acl(ctx, "live_chunks").toString()).contains("=r/");
            assertThat(PartitionScratch.acl(ctx, "collection_vector_stats").toString()).contains("=r/");
            // the views answer
            assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "live_chunks")))).isGreaterThan(0);
            assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "collection_vector_stats")))).isGreaterThan(0);
        }
    }

    @Test
    @Order(8)
    void keysConstraintsAndIndexNamesAreTheOldOnesOnTheNewTables_andTheOldTablesCarrySuffixedNames() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PartitionScratch.constraintDef(ctx, "chunks", "chunks_pk"))
                .isEqualTo("PRIMARY KEY (tenant_id, collection, chash, embedding_model)");
            assertThat(PartitionScratch.constraintDef(ctx, "chunks", "chunks_collection_fk"))
                .isEqualTo("FOREIGN KEY (tenant_id, collection, embedding_model) REFERENCES nexus.catalog_collections(tenant_id, name, embedding_model) ON DELETE RESTRICT");
            assertThat(PartitionScratch.constraintDef(ctx, "chunks", "chunks_chash_octet_check")).contains("NOT VALID");
            assertThat(PartitionScratch.constraintDef(ctx, "taxonomy_centroids", "taxonomy_centroids_pk"))
                .isEqualTo("PRIMARY KEY (tenant_id, collection, topic_id, embedding_model)");
            assertThat(PartitionScratch.constraintDef(ctx, "catalog_document_chunks", "fk_catalog_chunks_chunk"))
                .isEqualTo("FOREIGN KEY (tenant_id, collection, chash, embedding_model) REFERENCES nexus.chunks(tenant_id, collection, chash, embedding_model) ON UPDATE CASCADE DEFERRABLE");
            assertThat(PartitionScratch.constraintDef(ctx, "topic_assignments", "topic_assignments_chunk_fk"))
                .isEqualTo("FOREIGN KEY (tenant_id, source_collection, doc_id, embedding_model) REFERENCES nexus.chunks(tenant_id, collection, chash, embedding_model) ON UPDATE CASCADE ON DELETE CASCADE");
            assertThat(PartitionScratch.constraintDef(ctx, "chunk_orphaned_at", "chunk_orphaned_at_chunk_fk"))
                .isEqualTo("FOREIGN KEY (tenant_id, collection, chash, embedding_model) REFERENCES nexus.chunks(tenant_id, collection, chash, embedding_model) ON UPDATE CASCADE ON DELETE CASCADE");
            for (String fk : List.of("fk_catalog_chunks_chunk", "topic_assignments_chunk_fk", "chunk_orphaned_at_chunk_fk")) {
                assertThat(PgCatalogProbes.constraintValidated(ctx, fk)).as("%s validated", fk).isTrue();
            }
            for (String t : List.of("catalog_document_chunks", "topic_assignments", "chunk_orphaned_at")) {
                assertThat(PgCatalogProbes.columnNotNull(ctx, "nexus", t, "embedding_model")).as("%s.embedding_model NOT NULL", t).isTrue();
            }
            List<String> parentIdx = PgCatalogProbes.indexDefs(ctx, "nexus", "chunks");
            assertThat(parentIdx).hasSize(7).allSatisfy(d -> assertThat(d).doesNotContain("_new"));
            for (String old : List.of("chunks_pk", "idx_chunks_embedding_1024", "idx_chunks_embedding_384", "idx_chunks_embedding_768",
                    "idx_chunks_tenant_chash", "idx_chunks_trgm", "idx_chunks_tsv")) {
                assertThat(PgCatalogProbes.indexExists(ctx, "nexus", old)).as(old).isTrue();
                assertThat(PgCatalogProbes.indexExists(ctx, "nexus", old + "_retired_225")).as(old + "_retired_225").isTrue();
            }
            assertThat(PgCatalogProbes.indexDefs(ctx, "nexus", "taxonomy_centroids")).hasSize(4);
            assertThat(PgCatalogProbes.indexExists(ctx, "nexus", "taxonomy_centroids_pk_retired_225")).isTrue();
            // The old table's HNSW survived under its retired name and no relation is left under a staging name.
            assertThat(PgCatalogProbes.tablesInSchema(ctx, "nexus")).doesNotContain("chunks_new", "taxonomy_centroids_new");
            // The outbound key refuses a chunk filed under a model other than its collection's.
            assertThatThrownBy(() -> ctx.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, TA).set(CHUNKS.COLLECTION, A_CODE).set(CHUNKS.CHASH, Chash.ofText("wrong-model").toBytes())
                .set(CHUNKS.CHUNK_TEXT, "x").set(CHUNKS.EMBEDDING_MODEL, CONTEXT_3).set(CHUNKS.EMBEDDING_1024, vec(1024)).execute())
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("23503"));
            // a chash that is not 32 bytes: the NOT VALID check exempts only rows that existed, so a new write is refused
            assertThatThrownBy(() -> ctx.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, TA).set(CHUNKS.COLLECTION, A_CODE).set(CHUNKS.CHASH, new byte[31])
                .set(CHUNKS.CHUNK_TEXT, "x").set(CHUNKS.EMBEDDING_MODEL, CODE_3).set(CHUNKS.EMBEDDING_1024, vec(1024)).execute())
                .satisfies(t -> {
                    assertThat(sqlState(t)).isEqualTo("23514");
                    assertThat(String.valueOf(t.getMessage())).contains("chunks_chash_octet_check");
                });
            // and a vector of the wrong width for its model
            assertThatThrownBy(() -> ctx.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, TA).set(CHUNKS.COLLECTION, A_CODE).set(CHUNKS.CHASH, Chash.ofText("wrong-dim").toBytes())
                .set(CHUNKS.CHUNK_TEXT, "x").set(CHUNKS.EMBEDDING_MODEL, CODE_3).set(CHUNKS.EMBEDDING_768, vec(768)).execute())
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("23514"));
        }
    }

    @Test
    @Order(20)
    void statisticsExistOnEveryLeafAndBothParents_andAPlanPrunesToOneLeafAndUsesItsHnsw() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String parent : List.of("chunks", "taxonomy_centroids")) {
                for (var mp : PartitionScratch.children(ctx, parent)) {
                    for (var leaf : PartitionScratch.children(ctx, mp.name())) {
                        assertThat(reltuples(ctx, leaf.name())).as("ANALYZE ran on %s (reltuples is -1 until it does)", leaf.name())
                            .isGreaterThanOrEqualTo(0f);
                    }
                }
            }
            // A populated leaf has column statistics (the planner reads them, BUG-0148's failure mode).
            String populated = expectedName("chunks", CODE_3, TA);
            assertThat(statsRows(ctx, populated)).as("pg_stats rows of %s", populated).isGreaterThan(0);
            assertThat(statsRows(ctx, expectedName("taxonomy_centroids", CODE_3, TA))).isGreaterThan(0);
            // A plan against a leaf big enough for the planner to choose HNSW: seed one, ANALYZE, EXPLAIN.
            String coll = "knowledge__plan-owner__minilm-l6-v2-384__v1";
            PgContainerHelper.insertCollection(ctx, "p225-plan", coll);
            PgContainerHelper.seedServiceToken(ctx, "tok-p225-plan", "p225-plan", "p225-plan");   // the trigger makes its leaves
            assertThat(Routines.p225SeedRandomChunks(ctx.configuration(), "p225-plan", coll, MINILM_384, 384, 15000)).isEqualTo(15000L);
            PgContainerHelper.analyzeTable(su, CHUNKS);
            String plan = Routines.p225ExplainKnn(ctx.configuration(), "p225-plan", MINILM_384, 384);
            String leaf = expectedName("chunks", MINILM_384, "p225-plan");
            assertThat(plan).as(plan).contains(leaf);
            assertThat(plan).as("a plan with literal tenant and model reads exactly one leaf").doesNotContain(expectedName("chunks", MINILM_384, TA))
                .doesNotContain(expectedName("chunks", CODE_3, "p225-plan"));
            assertThat(plan).as("the leaf's HNSW index serves the ordering").contains("Index Scan using").contains("embedding_384_idx");
        }
    }

    @Test
    @Order(21)
    void theServiceTokensTriggerIsAttached_andANewTokenCreatesItsLeavesOnBothParents() throws Exception {
        try (Connection a = pg.createConnection("")) {
            DSLContext ctx = DSL.using(a, SQLDialect.POSTGRES);
            assertThat(PartitionScratch.triggersCalling(ctx, "service_tokens", "service_tokens_create_tenant_partitions")).isEqualTo(1);
            PgContainerHelper.seedServiceToken(ctx, "tok-p225-new", "p225-new", "p225-new");
            for (String parent : List.of("chunks", "taxonomy_centroids")) {
                for (String m : List.of(CODE_3, CONTEXT_3, BGE_768, MINILM_384)) {
                    assertThat(PartitionScratch.children(ctx, expectedName(parent, m, null)).stream().map(PartitionScratch.Child::name))
                        .as("%s/%s", parent, m).contains(expectedName(parent, m, "p225-new"));
                }
            }
            // The trigger-made leaf carries the parent's policies, grants and FORCE.
            String leaf = expectedName("chunks", CODE_3, "p225-new");
            assertThat(PgCatalogProbes.rowSecurity(ctx, "nexus", leaf).forced()).isTrue();
            assertThat(PartitionScratch.acl(ctx, leaf)).isEqualTo(PartitionScratch.acl(ctx, "chunks"));
        }
    }

    // ═══════════════════════════ TS3: the deferred manifest key against the partitioned parent ═══════════════════════════

    @Test
    @Order(22)
    void ts3a_thePurgeShape_committing_deletesChunksWhileManifestRowsExist_thenRemovesThem() throws Exception {
        String tenant = "p225-ts3a";
        String coll = "code__ts3a-owner__voyage-code-3__v1";
        String doc = "ts3a-doc";
        try (Connection a = pg.createConnection("")) {
            DSLContext su = DSL.using(a, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(su, "tok-p225-ts3a", tenant, "p225-ts3a");
            PgContainerHelper.insertCollection(su, tenant, coll);
            byte[] chash = Chash.ofText("ts3a-chunk").toBytes();
            su.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, coll).set(CHUNKS.CHASH, chash)
                .set(CHUNKS.CHUNK_TEXT, "ts3a").set(CHUNKS.EMBEDDING_MODEL, CODE_3).set(CHUNKS.EMBEDDING_1024, vec(1024)).execute();
            // a document tombstoned long ago, with a manifest row that names the chunk
            su.insertInto(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TENANT_ID, tenant)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TUMBLER, doc)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TITLE, "ts3a")
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, coll)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.DELETED_AT, OffsetDateTime.now().minusDays(400)).execute();
            su.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash)
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, coll).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CODE_3).execute();
        }
        // The real purge_trash (it defers fk_catalog_chunks_chunk itself) as the owner under the tenant GUC.
        try (Connection a = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASS)) {
            a.setAutoCommit(true);
            PgContainerHelper.setTenant(a, "nexus.tenant", tenant, false);
            DSLContext ctx = DSL.using(a, SQLDialect.POSTGRES);
            Long purged = dev.nexus.service.jooq.nexus.Routines.purgeTrash(ctx.configuration(),
                new org.jooq.types.YearToSecond(new org.jooq.types.YearToMonth(0, 0), new org.jooq.types.DayToSecond(30)));
            assertThat(purged).isEqualTo(1L);
        }
        try (Connection a = pg.createConnection("")) {
            DSLContext su = DSL.using(a, SQLDialect.POSTGRES);
            assertThat(su.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant))).as("the stranded chunk was swept").isZero();
            assertThat(su.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant))).isZero();
        }
    }

    @Test
    @Order(23)
    void ts3b_deleteAndReinsertOfTheChunkCommits_andTs3c_aDeleteThatLeavesTheManifestRowOrphanedFailsAtCommit() throws Exception {
        String tenant = "p225-ts3b";
        String coll = "code__ts3b-owner__voyage-code-3__v1";
        byte[] kept = Chash.ofText("ts3b-kept").toBytes();
        byte[] orphaned = Chash.ofText("ts3b-orphaned").toBytes();
        try (Connection a = pg.createConnection("")) {
            DSLContext su = DSL.using(a, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(su, "tok-p225-ts3b", tenant, "p225-ts3b");
            PgContainerHelper.insertCollection(su, tenant, coll);
            su.insertInto(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TENANT_ID, tenant)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TUMBLER, "ts3b-doc")
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.TITLE, "ts3b")
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, coll).execute();
            int pos = 0;
            for (byte[] c : List.of(kept, orphaned)) {
                su.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, coll).set(CHUNKS.CHASH, c)
                    .set(CHUNKS.CHUNK_TEXT, "ts3b").set(CHUNKS.EMBEDDING_MODEL, CODE_3).set(CHUNKS.EMBEDDING_1024, vec(1024)).execute();
                su.insertInto(CATALOG_DOCUMENT_CHUNKS)
                    .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, "ts3b-doc")
                    .set(CATALOG_DOCUMENT_CHUNKS.POSITION, pos++).set(CATALOG_DOCUMENT_CHUNKS.CHASH, c)
                    .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, coll).set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CODE_3).execute();
            }
        }
        try (Connection a = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASS)) {
            a.setAutoCommit(true);
            PgContainerHelper.setTenant(a, "nexus.tenant", tenant, false);
            DSLContext ctx = DSL.using(a, SQLDialect.POSTGRES);
            // (b) one statement, one transaction: delete and re-insert under a deferred key commits.
            assertThat(Routines.p225Ts3DeleteAndReinsert(ctx.configuration(), tenant, coll, kept)).isEqualTo(1);
            // (c) the same deferral, but the chunk is not put back: the manifest row is orphaned at COMMIT.
            assertThatThrownBy(() -> Routines.p225Ts3DeleteOnly(ctx.configuration(), tenant, coll, orphaned))
                .satisfies(t -> {
                    assertThat(sqlState(t)).isEqualTo("23503");
                    // Finding for the Java error mapping: a foreign key onto a PARTITIONED table is cloned onto the
                    // referencing table once per partition, and the violation names the auto-generated clone
                    // (catalog_document_chunks_..._fkeyN), never fk_catalog_chunks_chunk.
                    assertThat(String.valueOf(t.getMessage())).contains("violates foreign key constraint")
                        .contains("on table \"catalog_document_chunks\"").doesNotContain("\"fk_catalog_chunks_chunk\"");
                });
        }
        try (Connection a = pg.createConnection("")) {
            DSLContext su = DSL.using(a, SQLDialect.POSTGRES);
            assertThat(su.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant))).as("both chunks are still there").isEqualTo(2);
        }
    }

    @Test
    @Order(9)
    void theWalkWroteItsLockCountToTheServerLog() {
        String log = pg.getLogs();
        var lockLine = java.util.regex.Pattern.compile("rdr225 walk: (\\d+) relation lock").matcher(log);
        assertThat(lockLine.find()).isTrue();
        PartitionScratch.evidence("WALK relation locks held at the end of the walk: " + lockLine.group(1)
            + " (32 leaves: 4 tenants x 4 models x 2 parents; 12 chunks)");
        System.out.println("WALK_LOCKS " + lockLine.group(1));
        assertThat(log).contains("rdr225 walk:").contains("relation lock(s) held at the end of the walk");
    }

    // ═══════════════════════════ other scenarios (their own stores) ═══════════════════════════

    @Test
    void anInjectedFailureLateInTheSwapLeavesEveryTableUnchanged_andTheWalkSucceedsOnceTheBlockerIsGone() throws Exception {
        PostgreSQLContainer<?> c2 = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(c2, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(c2, 3)) {
                migrateUpTo(ds, WALK);
                List<String> before;
                try (Connection su = c2.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    seedStore(ctx);
                    // A relation squatting on the name step 7.2 gives the old HNSW index: the rename fails AFTER the
                    // copy, the indexes, the statistics and the reconciliation have all run.
                    ctx.createTable(DSL.name("nexus", "idx_chunks_tsv_retired_225")).column("x", SQLDataType.INTEGER).execute();
                    before = snapshot(ctx);
                }
                assertThatThrownBy(() -> applyRemaining(ds)).hasStackTraceContaining("idx_chunks_tsv_retired_225");
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(snapshot(ctx)).as("a failed walk changes nothing").isEqualTo(before);
                    assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "chunks_new")).isFalse();
                    assertThat(ctx.fetchCount(GC_AUDIT, GC_AUDIT.OPERATION.like("rdr225%"))).as("the audit rows rolled back too").isZero();
                    ctx.dropTable(DSL.name("nexus", "idx_chunks_tsv_retired_225")).execute();
                }
                applyRemaining(ds);
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(ctx.fetchCount(CHUNKS)).isEqualTo(12);
                }
            }
        } finally {
            c2.stop();
        }
    }

    @Test
    void aVectorWhoseDimensionDisagreesWithItsCollectionsModelFailsTheWalk_andLeavesEveryTableUnchanged() throws Exception {
        PostgreSQLContainer<?> c2 = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(c2, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(c2, 3)) {
                migrateUpTo(ds, WALK);
                List<String> before;
                try (Connection su = c2.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    seedStore(ctx);
                    // Legacy shape, not supported: a 768-wide vector in a collection registered under voyage-code-3 (1024).
                    chunk(ctx, TA, A_CODE, "a-code-wrong-dimension", 768);
                    before = snapshot(ctx);
                }
                // No pre-scan and no special message: the model partition's own dimension CHECK refuses the copy.
                assertThatThrownBy(() -> applyRemaining(ds))
                    .hasStackTraceContaining(expectedName("chunks", CODE_3, null) + "_dimension_chk");
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(snapshot(ctx)).as("a failed walk changes nothing: no table renamed, no row re-registered or moved").isEqualTo(before);
                    assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "chunks_new")).isFalse();
                    assertThat(ctx.fetchCount(CHUNKS)).as("every old row is still in the old table").isEqualTo(13);
                }
            }
        } finally {
            c2.stop();
        }
    }

    @Test
    void aSuperuserOwnedDiagView_isDroppedAndRecreatedOverTheNewTable_ownedByTheMigratingRole() throws Exception {
        PostgreSQLContainer<?> c2 = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(c2, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(c2, 3)) {
                migrateUpTo(ds, WALK);
                try (Connection su = c2.createConnection("")) {
                    su.setAutoCommit(true);
                    seedStore(DSL.using(su, SQLDialect.POSTGRES));
                    // A superuser-owned diag_chash_conformance, as pg_provision creates it: taxonomy-011-8 cannot
                    // replace it (it degrades to a skip), but the schema owner can still drop it.
                    PgContainerHelper.runSuperuserDdl(su, "ALTER VIEW nexus.diag_chash_conformance OWNER TO postgres");
                }
                applyRemaining(ds);
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.relationOwner(ctx, "nexus", "diag_chash_conformance")).isEqualTo(ADMIN);
                    assertThat(viewDependsOn(ctx, "diag_chash_conformance", "chunks")).isTrue();
                    assertThat(viewDependsOn(ctx, "diag_chash_conformance", "chunks_retired_225")).isFalse();
                    assertThat(PartitionScratch.acl(ctx, "diag_chash_conformance").toString())
                        .as("the previous owner is not turned into a grantee").doesNotContain("postgres=");
                    assertThat(ctx.fetchCount(CHUNKS)).isEqualTo(12);
                }
            }
        } finally {
            c2.stop();
        }
    }

    @Test
    void aFreshInstallHasTheDefaultTenantsLeaves_andTheTriggerAndMaintainOnEveryLeaf() throws Exception {
        PostgreSQLContainer<?> c2 = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(c2, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(c2, 3)) {
                SchemaMigrator.migrate(ds);
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String parent : List.of("chunks", "taxonomy_centroids")) {
                        var modelParts = PartitionScratch.children(ctx, parent);
                        assertThat(modelParts).as("four real models, no placeholder").hasSize(4);
                        for (var mp : modelParts) {
                            assertThat(PartitionScratch.children(ctx, mp.name()).stream().map(PartitionScratch.Child::name))
                                .hasSize(1).containsExactly(expectedName(parent, modelOf(mp.bound()), "default"));
                        }
                    }
                    assertThat(PartitionScratch.triggersCalling(ctx, "service_tokens", "service_tokens_create_tenant_partitions")).isEqualTo(1);
                    // MAINTAIN (the purge VACUUM) reaches the parent, every model partition and every leaf
                    // (grants-005 loops over the partition tree; the leaves were created before it ran).
                    for (var mp : PartitionScratch.children(ctx, "chunks")) {
                        assertThat(PartitionScratch.acl(ctx, mp.name()).toString()).as(mp.name()).contains("nexus_svc=arwdm/");
                        for (var leaf : PartitionScratch.children(ctx, mp.name())) {
                            assertThat(PartitionScratch.acl(ctx, leaf.name()).toString()).as(leaf.name()).contains("nexus_svc=arwdm/");
                        }
                    }
                }
            }
        } finally {
            c2.stop();
        }
    }

    @Test
    void aSecondBootReRunsTheRunAlwaysChangesets_andLeavesGrantsPoliciesViewAndATriggerMadeLeafIntact() throws Exception {
        PostgreSQLContainer<?> c2 = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(c2, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(c2, 3)) {
                SchemaMigrator.migrate(ds);
                String tenant = "p225-restart";
                String leaf = expectedName("chunks", CODE_3, tenant);
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    PgContainerHelper.seedServiceToken(ctx, "tok-p225-restart", tenant, "p225-restart");   // trigger: a leaf AFTER the walk
                }
                Map<String, List<String>> before;
                try (Connection su = c2.createConnection("")) {
                    before = accessShape(DSL.using(su, SQLDialect.POSTGRES), leaf);
                }
                var second = SchemaMigrator.migrate(ds);
                assertThat(second.reexecutedChangesets()).as("the runAlways changesets ran again").isGreaterThan(0);
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(accessShape(ctx, leaf)).as("leaf grants, RLS and policies survive a second boot").isEqualTo(before);
                    assertThat(accessShape(ctx, expectedName("chunks", CODE_3, "default")).get("acl").toString()).contains("nexus_svc=arwdm/");
                    assertThat(PgCatalogProbes.viewExists(ctx, "nexus", "diag_chash_conformance")).isTrue();
                    assertThat(viewDependsOn(ctx, "diag_chash_conformance", "chunks")).isTrue();
                    assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "diag_chash_conformance")))).isEqualTo(5);
                }
                // and nexus_svc can still write into the leaf the trigger made, as itself, under its tenant
                try (Connection svc = DriverManager.getConnection(c2.getJdbcUrl(), PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD)) {
                    svc.setAutoCommit(true);
                    PgContainerHelper.setTenant(svc, "nexus.tenant", tenant, false);
                    DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
                    try (Connection su = c2.createConnection("")) {
                        PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, "code__rs-owner__voyage-code-3__v1");
                    }
                    assertThat(ctx.insertInto(CHUNKS).set(CHUNKS.TENANT_ID, tenant).set(CHUNKS.COLLECTION, "code__rs-owner__voyage-code-3__v1")
                        .set(CHUNKS.CHASH, Chash.ofText("restart").toBytes()).set(CHUNKS.CHUNK_TEXT, "r")
                        .set(CHUNKS.EMBEDDING_MODEL, CODE_3).set(CHUNKS.EMBEDDING_1024, vec(1024)).execute()).isEqualTo(1);
                }
            }
        } finally {
            c2.stop();
        }
    }

    @Test
    void rollingBackTheChangesetRestoresTheOldLayout_andForwardReApplyRebuildsTheSameShape() throws Exception {
        PostgreSQLContainer<?> c2 = PgContainerHelper.startDedicated();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(c2, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(c2, 3)) {
                migrateUpTo(ds, WALK);
                List<String> preWalk;
                try (Connection su = c2.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    seedStore(ctx);
                    preWalk = oldLayoutShape(ctx);
                }
                applyRemaining(ds);
                List<String> walked;
                try (Connection su = c2.createConnection("")) {
                    walked = snapshot(DSL.using(su, SQLDialect.POSTGRES));
                }
                int depth;
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    Field<Integer> order = DSL.field(DSL.name("orderexecuted"), Integer.class);
                    Field<String> id = DSL.field(DSL.name("id"), String.class);
                    depth = ctx.selectCount().from(DSL.table(DSL.name("databasechangelog")))
                        .where(order.ge(ctx.select(order).from(DSL.table(DSL.name("databasechangelog"))).where(id.eq(WALK))))
                        .fetchOne(0, int.class);
                }
                rollback(ds, depth);
                try (Connection su = c2.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(oldLayoutShape(ctx)).as("the old layout is back, including its keys, names and views").isEqualTo(preWalk);
                    assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "chunks")))).as("the pre-walk rows are the ones that return").isEqualTo(12);
                }
                applyRemaining(ds);
                try (Connection su = c2.createConnection("")) {
                    assertThat(snapshot(DSL.using(su, SQLDialect.POSTGRES))).as("forward again rebuilds the same shape").isEqualTo(walked);
                }
            }
        } finally {
            c2.stop();
        }
    }

    // ═══════════════════════════ the seed ═══════════════════════════

    private static String h(String label) {
        return Chash.ofText("p225:" + label).toHex();
    }

    private static byte[] hb(String label) {
        return Chash.ofText("p225:" + label).toBytes();
    }

    private static Vector vec(int dim) {
        float[] f = new float[dim];
        Arrays.fill(f, 0.1f);
        f[0] = 0.5f;
        return Vector.of(f);
    }

    private static void chunk(DSLContext su, String tenant, String coll, String label, int dim) {
        switch (dim) {
            case 384 -> PgContainerHelper.insertChunk384(su, tenant, coll, hb(label), vec(384));
            case 768 -> PgContainerHelper.insertChunk768(su, tenant, coll, hb(label), vec(768));
            default -> PgContainerHelper.insertChunk1024(su, tenant, coll, hb(label), vec(1024));
        }
    }

    private static void centroid(DSLContext su, String tenant, String coll, long topicId, int dim) {
        var step = su.insertInto(TAXONOMY_CENTROIDS)
            .set(TAXONOMY_CENTROIDS.TENANT_ID, tenant).set(TAXONOMY_CENTROIDS.COLLECTION, coll)
            .set(TAXONOMY_CENTROIDS.TOPIC_ID, topicId).set(TAXONOMY_CENTROIDS.LABEL, "c" + topicId);
        switch (dim) {
            case 384 -> step.set(TAXONOMY_CENTROIDS.EMBEDDING_384, vec(384)).execute();
            case 768 -> step.set(TAXONOMY_CENTROIDS.EMBEDDING_768, vec(768)).execute();
            default -> step.set(TAXONOMY_CENTROIDS.EMBEDDING_1024, vec(1024)).execute();
        }
    }

    /** 12 chunks, 8 manifest rows, 2 assignments, 2 orphaned-at rows, 5 centroids. */
    static void seedStore(DSLContext su) {
        PgContainerHelper.seedServiceToken(su, "tok-p225-a", TA, "p225");
        PgContainerHelper.seedServiceToken(su, "tok-p225-b", TB, "p225");
        for (String[] tc : new String[][] {
            {TA, A_CODE}, {TA, A_CTX}, {TA, A_MINI}, {TB, B_CODE}, {TB, B_BGE}, {TN, N_CODE}}) {
            PgContainerHelper.insertCollection(su, tc[0], tc[1]);
        }
        // A_CODE: three 1024-d chunks, one of them hidden (no manifest row)
        chunk(su, TA, A_CODE, "a-code-1", 1024);
        chunk(su, TA, A_CODE, "a-code-2", 1024);
        chunk(su, TA, A_CODE, "a-code-hidden", 1024);
        PgContainerHelper.ownChunks(su, TA, A_CODE, h("a-code-1"), h("a-code-2"));
        // A_CTX
        chunk(su, TA, A_CTX, "a-ctx-1", 1024);
        chunk(su, TA, A_CTX, "a-ctx-2", 1024);
        PgContainerHelper.ownChunks(su, TA, A_CTX, h("a-ctx-1"), h("a-ctx-2"));
        // A_MINI: two 384-d chunks, both hidden, one marked orphaned
        chunk(su, TA, A_MINI, "a-mini-1", 384);
        chunk(su, TA, A_MINI, "a-mini-2", 384);
        // tenant B
        chunk(su, TB, B_CODE, "b-code-1", 1024);
        chunk(su, TB, B_CODE, "b-code-2", 1024);
        PgContainerHelper.ownChunks(su, TB, B_CODE, h("b-code-1"), h("b-code-2"));
        chunk(su, TB, B_BGE, "b-bge-1", 768);
        chunk(su, TB, B_BGE, "b-bge-2", 768);
        PgContainerHelper.ownChunks(su, TB, B_BGE, h("b-bge-1"), h("b-bge-2"));
        // a tenant with chunks and no token row
        chunk(su, TN, N_CODE, "n-code-1", 1024);

        // topic assignments on all three kinds of row
        su.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
            .values(9001L, TA, "p225-topic", A_CODE, 0, OffsetDateTime.now(), "pending").execute();
        for (Object[] a : new Object[][] {{"a-code-1", A_CODE}, {"a-ctx-1", A_CTX}}) {
            su.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID,
                    TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
                .values(TA, hb((String) a[0]), 9001L, "projection", (String) a[1], OffsetDateTime.now()).execute();
        }
        // orphaned-at rows (the stamp table): a hidden mini chunk and the notoken tenant's chunk
        su.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH,
                CHUNK_ORPHANED_AT.ORPHANED_AT).values(TA, A_MINI, hb("a-mini-1"), OffsetDateTime.now().minusDays(2)).execute();
        su.insertInto(CHUNK_ORPHANED_AT, CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH,
                CHUNK_ORPHANED_AT.ORPHANED_AT).values(TN, N_CODE, hb("n-code-1"), OffsetDateTime.now().minusDays(2)).execute();

        // centroids: 5 seeded, 4 copied
        centroid(su, TA, A_CODE, 1, 1024);
        centroid(su, TA, A_CODE, 2, 1024);
        centroid(su, TA, A_MINI, 4, 384);
        centroid(su, TA, GHOST, 7, 1024);       // no registry row: derived data, not copied
        centroid(su, TB, B_CODE, 1, 1024);
    }

    /** Manifest rows ownChunks wrote: 2 + 2 + 2 + 2. */
    private static int manifestRowsSeeded() {
        return 8;
    }

    // ═══════════════════════════ helpers ═══════════════════════════

    private static String modelOf(String bound) {
        return bound.replaceAll("^FOR VALUES IN \\('(.*)'\\)$", "$1");
    }

    private static String sqlState(Throwable t) {
        for (Throwable x = t; x != null; x = x.getCause()) {
            if (x instanceof DataAccessException d && d.sqlState() != null) return d.sqlState();
            if (x instanceof SQLException s && s.getSQLState() != null) return s.getSQLState();
        }
        return null;
    }

    private static String model(DSLContext ctx, String tenant, String coll) {
        return ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll)).fetchOne(0, String.class);
    }

    private static String state(DSLContext ctx, String tenant, String coll) {
        return ctx.select(CATALOG_COLLECTIONS.LIFECYCLE_STATE).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll)).fetchOne(0, String.class);
    }

    private static Field<String> tableoid() {
        return DSL.field(DSL.name("tableoid")).cast(org.jooq.impl.DefaultDataType.getDefaultDataType("regclass")).cast(String.class);
    }

    private static long retiredCount(DSLContext ctx, String table) {
        return ctx.fetchCount(DSL.table(DSL.name("nexus", table)));
    }

    private static float reltuples(DSLContext ctx, String rel) {
        return ctx.select(DSL.field(DSL.name("c", "reltuples"), Float.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(rel)).fetchOne(0, Float.class);
    }

    private static int statsRows(DSLContext ctx, String rel) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_stats")),
            DSL.field(DSL.name("schemaname"), String.class).eq("nexus").and(DSL.field(DSL.name("tablename"), String.class).eq(rel)));
    }

    /** True when the view's rewrite rule depends on the named nexus table. */
    private static boolean viewDependsOn(DSLContext ctx, String view, String table) {
        var d = DSL.table(DSL.name("pg_catalog", "pg_depend")).as("d");
        var r = DSL.table(DSL.name("pg_catalog", "pg_rewrite")).as("r");
        var v = DSL.table(DSL.name("pg_catalog", "pg_class")).as("v");
        var t = DSL.table(DSL.name("pg_catalog", "pg_class")).as("t");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.fetchExists(ctx.selectOne().from(d)
            .join(r).on(DSL.field(DSL.name("r", "oid")).eq(DSL.field(DSL.name("d", "objid"))))
            .join(v).on(DSL.field(DSL.name("v", "oid")).eq(DSL.field(DSL.name("r", "ev_class"))))
            .join(t).on(DSL.field(DSL.name("t", "oid")).eq(DSL.field(DSL.name("d", "refobjid"))))
            .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("t", "relnamespace"))))
            .where(DSL.field(DSL.name("v", "relname"), String.class).eq(view))
            .and(DSL.field(DSL.name("t", "relname"), String.class).eq(table))
            .and(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")));
    }

    /** RLS flags, policies and grants of one relation, as comparable text. */
    private static Map<String, List<String>> accessShape(DSLContext ctx, String rel) {
        Map<String, List<String>> m = new LinkedHashMap<>();
        var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", rel);
        m.put("rls", List.of(rls.enabled() + "/" + rls.forced()));
        m.put("policies", PartitionScratch.policies(ctx, rel).stream().map(Object::toString).toList());
        m.put("acl", new ArrayList<>(PartitionScratch.acl(ctx, rel)));
        return m;
    }

    /** What a failed or rolled-back walk must leave exactly as it found it. */
    private static List<String> oldLayoutShape(DSLContext ctx) {
        List<String> out = new ArrayList<>();
        for (String t : List.of("chunks", "taxonomy_centroids", "catalog_document_chunks", "topic_assignments", "chunk_orphaned_at")) {
            out.add("columns " + t + " " + PgCatalogProbes.columnNames(ctx, "nexus", t));
            out.add("count " + t + " " + ctx.fetchCount(DSL.table(DSL.name("nexus", t))));
            out.addAll(PgCatalogProbes.indexDefs(ctx, "nexus", t).stream().map(d -> "index " + d).sorted().toList());
        }
        for (String[] fk : new String[][] {{"catalog_document_chunks", "fk_catalog_chunks_chunk"}, {"topic_assignments", "topic_assignments_chunk_fk"},
                {"chunk_orphaned_at", "chunk_orphaned_at_chunk_fk"}, {"chunks", "chunks_collection_fk"}}) {
            out.add("fk " + fk[0] + "." + fk[1] + " " + PartitionScratch.constraintDef(ctx, fk[0], fk[1]));
        }
        out.add("kind chunks " + relkind(ctx, "chunks"));
        out.add("kind taxonomy_centroids " + relkind(ctx, "taxonomy_centroids"));
        for (String v : List.of("live_chunks", "collection_vector_stats", "diag_chash_conformance")) {
            out.add("view " + v + " " + PgCatalogProbes.viewExists(ctx, "nexus", v));
        }
        out.add("tables " + PgCatalogProbes.tablesInSchema(ctx, "nexus").stream().filter(t -> t.startsWith("chunks") || t.startsWith("taxonomy_centroids")).sorted().toList());
        return out;
    }

    private static String relkind(DSLContext ctx, String rel) {
        return ctx.select(DSL.field(DSL.name("c", "relkind"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(rel)).fetchOne(0, String.class);
    }

    /** Everything observable about the five tables, the views and the write-site functions, for before/after comparison. */
    private static List<String> snapshot(DSLContext ctx) {
        List<String> out = new ArrayList<>(oldLayoutShape(ctx));
        out.addAll(PgCatalogProbes.tablesInSchema(ctx, "nexus").stream().map(t -> "rel " + t + " " + relkind(ctx, t)).sorted().toList());
        for (String fn : List.of("gc_quarantine_orphans", "gc_quarantine_orphans_bounded", "gc_restore_rereferenced",
                "gc_restore_rereferenced_bounded", "reaper_quarantine_chunks", "quarantine_restore_chunks",
                "assign_from_chashes_384", "assign_from_chashes_768", "assign_from_chashes_1024",
                "stamp_chunks_on_manifest_delete", "stamp_chunks_on_manifest_update", "purge_trash")) {
            out.add("fn " + fn + " " + md5(String.valueOf(PgCatalogProbes.routineSignature(ctx, "nexus", fn)))
                + " " + routineBodyHash(ctx, fn));
        }
        out.add("trigger " + PartitionScratch.triggersCalling(ctx, "service_tokens", "service_tokens_create_tenant_partitions"));
        out.add("embedding_models " + ctx.select(EMBEDDING_MODELS.EMBEDDING_MODEL).from(EMBEDDING_MODELS).orderBy(EMBEDDING_MODELS.EMBEDDING_MODEL).fetch(0));
        out.add("registry " + ctx.select(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.EMBEDDING_MODEL)
            .from(CATALOG_COLLECTIONS).orderBy(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME).fetch().formatCSV());
        return out;
    }

    private static String routineBodyHash(DSLContext ctx, String fn) {
        return md5(ctx.select(DSL.field(DSL.name("p", "prosrc"), String.class))
            .from(DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p"))
            .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
            .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("p", "proname"), String.class).eq(fn))
            .orderBy(DSL.field(DSL.name("p", "oid")))
            .fetch(0, String.class).toString());
    }

    private static String md5(String s) {
        try {
            var d = java.security.MessageDigest.getInstance("MD5").digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : d) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static HikariDataSource pool(PostgreSQLContainer<?> c, int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(c.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(size);
        return new HikariDataSource(cfg);
    }

    private static void applyRemaining(HikariDataSource ds) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), database)) {
                lb.update(new Contexts(), new LabelExpression());
            }
        }
    }

    private static void rollback(HikariDataSource ds, int rows) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), database)) {
                lb.rollback(rows, new Contexts(), new LabelExpression());
            }
        }
    }

    /** Applies the changelog up to, not including, {@code target} (the hygiene walk tests' own idiom). */
    private static void migrateUpTo(HikariDataSource ds, String target) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = lb.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (target.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as(target + " must be in the master changelog").isGreaterThanOrEqualTo(0);
                lb.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
