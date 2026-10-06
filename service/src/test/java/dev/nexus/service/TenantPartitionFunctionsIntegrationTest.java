// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import static dev.nexus.service.PartitionScratch.BGE_768;
import static dev.nexus.service.PartitionScratch.CENTROIDS_NEW;
import static dev.nexus.service.PartitionScratch.CHUNKS_NEW;
import static dev.nexus.service.PartitionScratch.CODE_3;
import static dev.nexus.service.PartitionScratch.CONTEXT_3;
import static dev.nexus.service.PartitionScratch.expectedName;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 P1.2 (nexus-3wh8d.7): the objects of {@code vectors-030} (the UNIQUE on catalog_collections, the
 * shared partition naming function, {@code create_model_partition}, {@code create_tenant_partitions}, their
 * helpers and the unattached trigger function), tested on scratch partitioned parents that are faithful to
 * the migration's target: the live column set, the live parent-level indexes (three HNSW, two GIN, one
 * btree, so a leaf pays real index cost), the production RLS policies and grants, three referencing tables
 * with 4-column foreign keys onto the parent, and the composite outbound foreign key onto catalog_collections.
 *
 * <p><b>The migrating role is a non-superuser owner</b> (production's nexus_admin shape), through a
 * dedicated container, so the SECURITY DEFINER function runs as an owner who is subject to row-level
 * security and holds no BYPASSRLS. A superuser-migrated template would let a definer function bypass
 * everything and prove nothing about production (vectors-029's header makes the same point).
 *
 * <p>Lock behaviour is measured here and the numbers go to {@code target/p225-evidence.txt}; the RDR's
 * tenant-creation wording is written from them. Time bounds in the assertions are loose on purpose (a
 * busy box must not flake them); the evidence file carries the real figures.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantPartitionFunctionsIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_p225";
    private static final String ADMIN_PASS = "nexus_admin_p225_pass";

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(3);
        adminDs = new HikariDataSource(cfg);
        SchemaMigrator.migrate(adminDs);
    }

    @AfterAll
    void stopAll() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void resetScratch() throws Exception {
        try (Connection c = adminDs.getConnection()) {
            PartitionScratch.reset(c);
        }
    }

    // ── connections ──────────────────────────────────────────────────────────

    private Connection admin() throws SQLException {
        return DriverManager.getConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
    }

    private Connection svc() throws SQLException {
        return DriverManager.getConnection(pg.getJdbcUrl(), PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD);
    }

    private static DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    private static String sqlState(Throwable t) {
        for (Throwable x = t; x != null; x = x.getCause()) {
            if (x instanceof DataAccessException d && d.sqlState() != null) return d.sqlState();
            if (x instanceof SQLException s && s.getSQLState() != null) return s.getSQLState();
        }
        return null;
    }

    private static List<String> names(List<PartitionScratch.Child> kids) {
        return kids.stream().map(PartitionScratch.Child::name).toList();
    }

    private static String bound(String tenant) {
        return "FOR VALUES IN ('" + tenant.replace("'", "''") + "')";
    }

    // ── 1. the UNIQUE ────────────────────────────────────────────────────────

    @Test
    void uniqueOnCatalogCollections_exists_andBacksTheCompositeForeignKey() throws Exception {
        try (Connection c = admin()) {
            var ctx = dsl(c);
            assertThat(PartitionScratch.constraintDef(ctx, "catalog_collections", "catalog_collections_tenant_name_model_uq"))
                .isEqualTo("UNIQUE (tenant_id, name, embedding_model)");
            // The scratch parent's outbound FK references exactly that key; it could not have been created without it.
            assertThat(PartitionScratch.constraintDef(ctx, "chunks_new", "chunks_new_collection_fk"))
                .contains("REFERENCES nexus.catalog_collections(tenant_id, name, embedding_model)");
        }
    }

    // ── 2. the naming function ───────────────────────────────────────────────

    @Test
    void partitionName_matchesTheIndependentOracle_stripsNew_andFitsTheLimit() throws Exception {
        try (Connection c = admin()) {
            var ctx = dsl(c);
            for (String parent : List.of("chunks", "chunks_new", "taxonomy_centroids", "taxonomy_centroids_new")) {
                for (String model : List.of(CODE_3, CONTEXT_3, BGE_768, PartitionScratch.MINILM_384, "disputed-1024")) {
                    assertThat(PartitionScratch.partitionName(ctx, parent, model, null))
                        .isEqualTo(expectedName(parent, model, null));
                    for (String tenant : List.of("default", "alpha", "o'brien\\x", "", "tenant-with-a-fairly-long-identifier-0123456789")) {
                        String got = PartitionScratch.partitionName(ctx, parent, model, tenant);
                        assertThat(got).isEqualTo(expectedName(parent, model, tenant));
                        assertThat(got.getBytes(java.nio.charset.StandardCharsets.UTF_8).length).isLessThanOrEqualTo(63);
                    }
                }
            }
            // The _new suffix is stripped: leaves made under chunks_new carry the post-swap names.
            assertThat(PartitionScratch.partitionName(ctx, "chunks_new", CODE_3, "alpha"))
                .isEqualTo(PartitionScratch.partitionName(ctx, "chunks", CODE_3, "alpha"));
            // The longest name the layout produces, 47 bytes, is under the 63-byte limit with room to spare.
            assertThat(PartitionScratch.partitionName(ctx, "taxonomy_centroids_new", CODE_3, "alpha")).hasSize(47);
            // A null model has no name.
            assertThatThrownBy(() -> PartitionScratch.partitionName(ctx, "chunks", null, "alpha"))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22004"));
        }
    }

    // ── 3. create_model_partition ────────────────────────────────────────────

    @Test
    void createModelPartition_chunksShaped_yieldsPartitionCheckAndALeafPerExistingTenant() throws Exception {
        try (Connection c = admin()) {
            var ctx = dsl(c);
            PgContainerHelper.seedServiceToken(ctx, "tok-alpha-p225", "alpha", "p225");
            PgContainerHelper.seedServiceToken(ctx, "tok-beta-p225", "beta", "p225");
            String mp = expectedName(CHUNKS_NEW, CODE_3, null);

            String returned = PartitionScratch.createModelPartition(ctx, CHUNKS_NEW, CODE_3, true);

            assertThat(returned).endsWith(mp);
            var modelParts = PartitionScratch.children(ctx, CHUNKS_NEW);
            assertThat(modelParts).containsExactly(new PartitionScratch.Child(mp, bound(CODE_3)));
            assertThat(PartitionScratch.partKeyDef(ctx, mp)).isEqualTo("LIST (tenant_id)");
            String check = PartitionScratch.constraintDef(ctx, mp, mp + "_dimension_chk");
            assertThat(check).contains("embedding_1024 IS NOT NULL").contains("embedding_384 IS NULL")
                .contains("embedding_768 IS NULL");
            var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", mp);
            assertThat(rls.enabled()).isTrue();
            assertThat(rls.forced()).isTrue();

            var leaves = PartitionScratch.children(ctx, mp);
            assertThat(names(leaves)).containsExactlyInAnyOrder(
                expectedName(CHUNKS_NEW, CODE_3, "alpha"), expectedName(CHUNKS_NEW, CODE_3, "beta"),
                expectedName(CHUNKS_NEW, CODE_3, "default"));
            for (var leaf : leaves) {
                var f = PgCatalogProbes.rowSecurity(ctx, "nexus", leaf.name());
                assertThat(f.enabled()).as(leaf.name()).isTrue();
                assertThat(f.forced()).as(leaf.name()).isTrue();
            }

            // Idempotent: a second call creates nothing and returns the same partition.
            int before = PartitionScratch.nexusRelationCount(ctx);
            assertThat(PartitionScratch.createModelPartition(ctx, CHUNKS_NEW, CODE_3, true)).endsWith(mp);
            assertThat(PartitionScratch.nexusRelationCount(ctx)).isEqualTo(before);
        }
        // The CHECK refuses a row whose vector dimension disagrees with its model, and the right shape lands in
        // the tenant's leaf. Run as nexus_svc inside the tenant, so row-level security applies.
        try (Connection c = svc()) {
            c.setAutoCommit(true);
            var ctx = dsl(c);
            PgContainerHelper.setTenant(c, "nexus.tenant", "alpha", false);
            String coll = "code__p225__voyage-code-3__v1";
            PgContainerHelper.insertCollection(ctx, "alpha", coll);
            String mp = expectedName(CHUNKS_NEW, CODE_3, null);
            assertThatThrownBy(() -> PartitionScratch.insertChunk(ctx, "alpha", coll, new byte[32], CODE_3, 768))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("23514"))
                .hasMessageContaining(mp + "_dimension_chk");
            assertThat(PartitionScratch.insertChunk(ctx, "alpha", coll, new byte[32], CODE_3, 1024)).isEqualTo(1);
            assertThat(PartitionScratch.leafHolding(ctx, CHUNKS_NEW, "alpha"))
                .endsWith(expectedName(CHUNKS_NEW, CODE_3, "alpha"));
        }
    }

    @Test
    void createModelPartition_taxonomyCentroidsShaped_yieldsPartitionCheckAndALeafPerExistingTenant() throws Exception {
        try (Connection c = admin()) {
            var ctx = dsl(c);
            PgContainerHelper.seedServiceToken(ctx, "tok-alpha-p225", "alpha", "p225");
            String mp = expectedName(CENTROIDS_NEW, BGE_768, null);

            assertThat(PartitionScratch.createModelPartition(ctx, CENTROIDS_NEW, BGE_768, true)).endsWith(mp);

            assertThat(PartitionScratch.children(ctx, CENTROIDS_NEW))
                .containsExactly(new PartitionScratch.Child(mp, bound(BGE_768)));
            assertThat(PartitionScratch.constraintDef(ctx, mp, mp + "_dimension_chk"))
                .contains("embedding_768 IS NOT NULL").contains("embedding_384 IS NULL").contains("embedding_1024 IS NULL");
            assertThat(names(PartitionScratch.children(ctx, mp))).containsExactlyInAnyOrder(
                expectedName(CENTROIDS_NEW, BGE_768, "alpha"), expectedName(CENTROIDS_NEW, BGE_768, "default"));
        }
        try (Connection c = svc()) {
            c.setAutoCommit(true);
            var ctx = dsl(c);
            PgContainerHelper.setTenant(c, "nexus.tenant", "alpha", false);
            assertThatThrownBy(() -> PartitionScratch.insertCentroid(ctx, "alpha", "coll", 1L, BGE_768, 1024))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("23514"));
            assertThat(PartitionScratch.insertCentroid(ctx, "alpha", "coll", 1L, BGE_768, 768)).isEqualTo(1);
            assertThat(PartitionScratch.leafHolding(ctx, CENTROIDS_NEW, "alpha"))
                .endsWith(expectedName(CENTROIDS_NEW, BGE_768, "alpha"));
        }
    }

    @Test
    void createModelPartition_refusesAnUnknownModel_andALaterModelCoversTenantsAddedSince() throws Exception {
        try (Connection c = admin()) {
            var ctx = dsl(c);
            assertThatThrownBy(() -> PartitionScratch.createModelPartition(ctx, CHUNKS_NEW, "no-such-model", true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22023"));

            PgContainerHelper.seedServiceToken(ctx, "tok-alpha-p225", "alpha", "p225");
            PartitionScratch.createModelPartition(ctx, CHUNKS_NEW, CODE_3, true);
            PgContainerHelper.seedServiceToken(ctx, "tok-gamma-p225", "gamma", "p225");   // no function call for gamma
            PartitionScratch.createModelPartition(ctx, CHUNKS_NEW, CONTEXT_3, true);

            assertThat(names(PartitionScratch.children(ctx, expectedName(CHUNKS_NEW, CODE_3, null))))
                .doesNotContain(expectedName(CHUNKS_NEW, CODE_3, "gamma"));
            assertThat(names(PartitionScratch.children(ctx, expectedName(CHUNKS_NEW, CONTEXT_3, null))))
                .containsExactlyInAnyOrder(
                    expectedName(CHUNKS_NEW, CONTEXT_3, "alpha"), expectedName(CHUNKS_NEW, CONTEXT_3, "gamma"),
                    expectedName(CHUNKS_NEW, CONTEXT_3, "default"));
        }
    }

    // ── 4. create_tenant_partitions ──────────────────────────────────────────

    private void twoModelsOnBothParents() throws Exception {
        try (Connection c = admin()) {
            var ctx = dsl(c);
            for (String parent : List.of(CHUNKS_NEW, CENTROIDS_NEW)) {
                PartitionScratch.createModelPartition(ctx, parent, CODE_3, true);
                PartitionScratch.createModelPartition(ctx, parent, BGE_768, true);
            }
        }
    }

    @Test
    void createTenantPartitions_asNexusSvc_makesALeafPerModelPartitionOwnedBySchemaOwner() throws Exception {
        twoModelsOnBothParents();
        try (Connection c = svc(); Connection a = admin()) {
            var ctx = dsl(c);
            var actx = dsl(a);
            assertThat(PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "newco", true)).isEqualTo(2);
            assertThat(PartitionScratch.createTenantPartitions(ctx, CENTROIDS_NEW, "newco", true)).isEqualTo(2);

            for (String parent : List.of(CHUNKS_NEW, CENTROIDS_NEW)) {
                for (String model : List.of(CODE_3, BGE_768)) {
                    String mp = expectedName(parent, model, null);
                    String leaf = expectedName(parent, model, "newco");
                    assertThat(PartitionScratch.children(actx, mp))
                        .contains(new PartitionScratch.Child(leaf, bound("newco")));
                    // SECURITY DEFINER: the leaf belongs to the schema owner, not to nexus_svc who called.
                    assertThat(PartitionScratch.owner(actx, leaf)).isEqualTo(ADMIN_ROLE);
                    var f = PgCatalogProbes.rowSecurity(actx, "nexus", leaf);
                    assertThat(f.enabled()).isTrue();
                    assertThat(f.forced()).isTrue();
                    // Policies are the parent's own, name for name, qual for qual.
                    assertThat(PartitionScratch.policies(actx, leaf)).isEqualTo(PartitionScratch.policies(actx, parent));
                    // Grants are the parent's own, MAINTAIN included.
                    assertThat(PartitionScratch.acl(actx, leaf)).isEqualTo(PartitionScratch.acl(actx, parent));
                }
            }
            assertThat(PartitionScratch.policies(actx, CHUNKS_NEW))
                .extracting(PartitionScratch.PolicyRow::name)
                .containsExactlyInAnyOrder("tenant_isolation", "chunks_gate_probe_owner_read");
            assertThat(PartitionScratch.acl(actx, CHUNKS_NEW)).contains("nexus_svc=arwdm/" + ADMIN_ROLE);
            assertThat(PartitionScratch.acl(actx, expectedName(CHUNKS_NEW, CODE_3, "newco")))
                .contains("nexus_svc=arwdm/" + ADMIN_ROLE);
        }
    }

    @Test
    void createTenantPartitions_isIdempotent_byBound_includingATenantNeedingQuoting() throws Exception {
        twoModelsOnBothParents();
        try (Connection c = svc(); Connection a = admin()) {
            var ctx = dsl(c);
            var actx = dsl(a);
            for (String tenant : List.of("newco", "o'brien\\back", "")) {
                assertThat(PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, tenant, true))
                    .as("first call for '%s'", tenant).isEqualTo(2);
                int before = PartitionScratch.nexusRelationCount(actx);
                assertThat(PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, tenant, true))
                    .as("second call for '%s'", tenant).isZero();
                assertThat(PartitionScratch.nexusRelationCount(actx))
                    .as("second call for '%s' adds no relation", tenant).isEqualTo(before);
            }
            assertThat(PartitionScratch.children(actx, expectedName(CHUNKS_NEW, CODE_3, null)))
                .contains(new PartitionScratch.Child(expectedName(CHUNKS_NEW, CODE_3, "o'brien\\back"),
                    bound("o'brien\\back")));
        }
    }

    @Test
    void aCallForATenantThatHasEveryLeaf_takesNoLock_whileOneThatMustCreateWaitsOnTheTenantsAdvisoryLock() throws Exception {
        twoModelsOnBothParents();
        try (Connection c = svc(); Connection holder = admin()) {
            var ctx = dsl(c);
            assertThat(PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "idem", true)).isEqualTo(2);
            // Another session holds the tenant's advisory lock in an open transaction (a concurrent first creation).
            holder.setAutoCommit(false);
            dsl(holder).select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtextextended", Long.class,
                    DSL.inline("nexus.create_tenant_partitions:idem"), DSL.inline(0L)))).fetch();
            // chunks_new already has every leaf for the tenant: the fast path is one catalog query and takes no lock.
            long t0 = System.nanoTime();
            assertThat(PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "idem", true)).isZero();
            assertThat((System.nanoTime() - t0) / 1_000_000).as("ms for the no-op call").isLessThan(1000L);
            // taxonomy_centroids_new has none yet: that call must create, so it queues on the advisory lock and times out.
            long t1 = System.nanoTime();
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(ctx, CENTROIDS_NEW, "idem", true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("55P03"));
            assertThat((System.nanoTime() - t1) / 1_000_000).as("ms until the creating call timed out").isBetween(1800L, 4500L);
            holder.rollback();
        }
    }

    @Test
    void createTenantPartitions_withForceFalse_enablesButDoesNotForceRowLevelSecurity() throws Exception {
        twoModelsOnBothParents();
        try (Connection c = svc(); Connection a = admin()) {
            PartitionScratch.createTenantPartitions(dsl(c), CHUNKS_NEW, "loose", false);
            var actx = dsl(a);
            for (String model : List.of(CODE_3, BGE_768)) {
                var f = PgCatalogProbes.rowSecurity(actx, "nexus", expectedName(CHUNKS_NEW, model, "loose"));
                assertThat(f.enabled()).isTrue();
                assertThat(f.forced()).isFalse();
            }
            // The default is forced.
            PartitionScratch.createTenantPartitions(dsl(c), CHUNKS_NEW, "tight", true);
            assertThat(PgCatalogProbes.rowSecurity(actx, "nexus", expectedName(CHUNKS_NEW, CODE_3, "tight")).forced()).isTrue();
        }
    }

    @Test
    void createTenantPartitions_collisionWithAnotherTenantsLeafName_isRefused_andNothingIsLeftBehind() throws Exception {
        twoModelsOnBothParents();
        String victimLeaf = expectedName(CHUNKS_NEW, CODE_3, "victim");
        String mp = expectedName(CHUNKS_NEW, CODE_3, null);
        try (Connection a = admin()) {
            // Deterministic collision: a relation already holds the name the victim's leaf would get, bound to
            // a different tenant.
            PgContainerHelper.runSuperuserDdl(a, "CREATE TABLE nexus." + victimLeaf
                + " PARTITION OF nexus." + mp + " FOR VALUES IN ('squatter')");
        }
        try (Connection c = svc(); Connection a = admin()) {
            var ctx = dsl(c);
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "victim", true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("42P07"))
                .hasMessageContaining("partition name collision");
            // The refused call is one transaction: the victim has no leaf under the other model either.
            assertThat(names(PartitionScratch.children(dsl(a), expectedName(CHUNKS_NEW, BGE_768, null))))
                .doesNotContain(expectedName(CHUNKS_NEW, BGE_768, "victim"));
            // The squatter still holds its leaf.
            assertThat(PartitionScratch.children(dsl(a), mp))
                .contains(new PartitionScratch.Child(victimLeaf, bound("squatter")));
        }
    }

    @Test
    void createTenantPartitions_isDefinerWithFixedSearchPath_executableByNexusSvcOnly() throws Exception {
        try (Connection a = admin()) {
            var ctx = dsl(a);
            String sig = "nexus.create_tenant_partitions(regclass, text, boolean)";
            assertThat(PgCatalogProbes.routineSecurityDefiner(ctx, "nexus", "create_tenant_partitions")).isTrue();
            assertThat(PartitionScratch.procConfig(ctx, "create_tenant_partitions"))
                .contains("search_path=pg_catalog, pg_temp").contains("lock_timeout=2s");
            assertThat(PgCatalogProbes.canExecuteFunction(ctx, "nexus_svc", sig)).isTrue();
            assertThat(PgCatalogProbes.hasExplicitRoutineGrant(ctx, "nexus", "create_tenant_partitions", "nexus_svc", "EXECUTE")).isTrue();
            // PUBLIC has none: a role with no grant of its own (pg_monitor) cannot call it, and no PUBLIC ACL row exists.
            assertThat(PgCatalogProbes.canExecuteFunction(ctx, "pg_monitor", sig)).isFalse();
            assertThat(PgCatalogProbes.hasExplicitRoutineGrant(ctx, "nexus", "create_tenant_partitions", "PUBLIC", "EXECUTE")).isFalse();
            // The model-partition function, the helpers and the trigger function are not reachable by nexus_svc.
            for (String other : List.of(
                    "nexus.create_model_partition(regclass, text, boolean)",
                    "nexus.partition_sync_access(regclass, boolean)",
                    "nexus.partition_ensure_leaf(regclass, regclass, text, boolean)",
                    "nexus.partition_copy_access(regclass, regclass, boolean)",
                    "nexus.partition_parent_check(regclass)",
                    "nexus.partition_bound_value(regclass)",
                    "nexus.partition_name(text, text, text)",
                    "nexus.service_tokens_create_tenant_partitions()")) {
                assertThat(PgCatalogProbes.canExecuteFunction(ctx, "nexus_svc", other)).as(other).isFalse();
                assertThat(PgCatalogProbes.canExecuteFunction(ctx, "pg_monitor", other)).as(other).isFalse();
            }
        }
    }

    @Test
    void createTenantPartitions_restoresTheCallersLockTimeout_whenItReturns() throws Exception {
        twoModelsOnBothParents();
        try (Connection c = svc()) {
            c.setAutoCommit(false);
            var ctx = dsl(c);
            String before = PartitionScratch.setting(ctx, "lock_timeout");
            PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "restore", true);
            // A function-level SET reverts at return; SET LOCAL would have left 2s on the rest of the token insert.
            assertThat(PartitionScratch.setting(ctx, "lock_timeout")).isEqualTo(before);
            c.rollback();
        }
    }

    @Test
    void parentCheck_refusesAnythingButTheFourServedParents() throws Exception {
        try (Connection a = admin()) {
            var ctx = dsl(a);
            PgContainerHelper.runSuperuserDdl(a,
                "CREATE TABLE nexus.t225_other_part (tenant_id text, embedding_model text) PARTITION BY LIST (embedding_model)");
            // Right shape, wrong name.
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(ctx, "t225_other_part", "x", true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22023"));
            // Right name, not partitioned (the live table, until the migration swaps it).
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(ctx, "chunks", "x", true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("42809"));
            assertThatThrownBy(() -> PartitionScratch.createModelPartition(ctx, "taxonomy_centroids", CODE_3, true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("42809"));
            // An unrelated table.
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(ctx, "catalog_collections", "x", true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22023"));
            // A null tenant.
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, null, true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("22004"));
        }
    }

    // ── 5. the copy is the parent's catalog entries, not a fixed list ────────

    @Test
    void aNewLeafMirrorsPolicyAttributesAndGrantsTheParentHasNow_andSyncReMirrorsExistingChildren() throws Exception {
        try (Connection a = admin()) {
            var ctx = dsl(a);
            // A parent policy the production tables do not have: restrictive, per-command, role-scoped; and grants
            // beyond nexus_svc's, one of them with GRANT OPTION.
            PgContainerHelper.runSuperuserDdl(a,
                "CREATE POLICY t225_extra ON nexus.chunks_new AS RESTRICTIVE FOR SELECT TO nexus_svc USING (collection <> '')");
            PgContainerHelper.runSuperuserDdl(a, "GRANT SELECT ON nexus.chunks_new TO PUBLIC");
            PgContainerHelper.runSuperuserDdl(a, "GRANT UPDATE ON nexus.chunks_new TO pg_monitor WITH GRANT OPTION");
            PartitionScratch.createModelPartition(ctx, CHUNKS_NEW, CODE_3, true);
            PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "mirror", true);
            String mp = expectedName(CHUNKS_NEW, CODE_3, null);
            String leaf = expectedName(CHUNKS_NEW, CODE_3, "mirror");

            var parentPolicies = PartitionScratch.policies(ctx, CHUNKS_NEW);
            assertThat(parentPolicies).extracting(PartitionScratch.PolicyRow::name)
                .contains("t225_extra");
            var extra = parentPolicies.stream().filter(p -> p.name().equals("t225_extra")).findFirst().orElseThrow();
            assertThat(extra.permissive()).isEqualTo("RESTRICTIVE");
            assertThat(extra.cmd()).isEqualTo("SELECT");
            assertThat(extra.roles()).isEqualTo("nexus_svc");
            for (String child : List.of(mp, leaf)) {
                assertThat(PartitionScratch.policies(ctx, child)).as(child).isEqualTo(parentPolicies);
                assertThat(PartitionScratch.acl(ctx, child)).as(child).isEqualTo(PartitionScratch.acl(ctx, CHUNKS_NEW));
            }
            assertThat(PartitionScratch.acl(ctx, leaf)).anyMatch(e -> e.startsWith("=r/"))          // PUBLIC SELECT
                .anyMatch(e -> e.startsWith("pg_monitor=w*/"));                                      // UPDATE, grant option

            // The parent loses the extras; sync removes them from the children too.
            PgContainerHelper.runSuperuserDdl(a, "DROP POLICY t225_extra ON nexus.chunks_new");
            PgContainerHelper.runSuperuserDdl(a, "REVOKE SELECT ON nexus.chunks_new FROM PUBLIC");
            PgContainerHelper.runSuperuserDdl(a, "REVOKE UPDATE ON nexus.chunks_new FROM pg_monitor");
            assertThat(PartitionScratch.syncAccess(ctx, CHUNKS_NEW, true)).isEqualTo(3);   // the model partition and its two leaves, mirror and default
            for (String child : List.of(mp, leaf)) {
                assertThat(PartitionScratch.policies(ctx, child)).as(child).isEqualTo(PartitionScratch.policies(ctx, CHUNKS_NEW));
                assertThat(PartitionScratch.acl(ctx, child)).as(child).isEqualTo(PartitionScratch.acl(ctx, CHUNKS_NEW));
            }

            // Sync also carries the FORCE flag: the walk passes false for steps 2 to 6 and restores true at 7.7.
            PartitionScratch.syncAccess(ctx, CHUNKS_NEW, false);
            for (String child : List.of(mp, leaf)) {
                assertThat(PgCatalogProbes.rowSecurity(ctx, "nexus", child).forced()).as(child).isFalse();
            }
            PartitionScratch.syncAccess(ctx, CHUNKS_NEW, true);
            for (String child : List.of(mp, leaf)) {
                assertThat(PgCatalogProbes.rowSecurity(ctx, "nexus", child).forced()).as(child).isTrue();
            }
        }
    }

    // ── 6. the trigger function: defined, not attached ───────────────────────

    @Test
    void triggerFunction_createsLeavesForAnInsertedTenant_onceAttached_andIsNotAttachedToServiceTokens() throws Exception {
        twoModelsOnBothParents();
        try (Connection a = admin()) {
            // Attach it to the scratch stand-in for service_tokens, naming the scratch parents.
            PgContainerHelper.runSuperuserDdl(a,
                "CREATE TRIGGER t225_tokens_tenant AFTER INSERT ON nexus.t225_tokens FOR EACH ROW "
                    + "EXECUTE FUNCTION nexus.service_tokens_create_tenant_partitions('nexus.chunks_new', 'nexus.taxonomy_centroids_new')");
        }
        try (Connection c = svc(); Connection a = admin()) {
            var ctx = dsl(c);
            var actx = dsl(a);
            ctx.insertInto(DSL.table(DSL.name("nexus", "t225_tokens")))
                .set(DSL.field(DSL.name("tenant_id"), String.class), "trig-tenant").execute();
            for (String parent : List.of(CHUNKS_NEW, CENTROIDS_NEW)) {
                for (String model : List.of(CODE_3, BGE_768)) {
                    assertThat(names(PartitionScratch.children(actx, expectedName(parent, model, null))))
                        .contains(expectedName(parent, model, "trig-tenant"));
                }
            }
            int before = PartitionScratch.nexusRelationCount(actx);
            ctx.insertInto(DSL.table(DSL.name("nexus", "t225_tokens")))
                .set(DSL.field(DSL.name("tenant_id"), String.class), "trig-tenant").execute();
            assertThat(PartitionScratch.nexusRelationCount(actx)).as("a second token for the tenant adds nothing").isEqualTo(before);
            // The trigger function runs as the inserting role: nexus_svc has EXECUTE on create_tenant_partitions only.
            // And the live service_tokens carries no such trigger until migration step 7.7 (bead nexus-3wh8d.8
            // flips this assertion when it attaches it).
            assertThat(PartitionScratch.triggersCalling(actx, "service_tokens", "service_tokens_create_tenant_partitions")).isZero();
        }
    }

    // ── 7. locks, timeouts and what a pending creation does to other writers ─

    private static final long LOCK_TIMEOUT_MS = 2000;
    private static final String CODE_COLL = "code__p225__voyage-code-3__v1";
    private static final String BGE_COLL = "knowledge__p225__bge-base-en-v15-768__v1";

    /** Model partitions of the scratch chunks parent in the order the creation visits them (by relname). */
    private static List<String> modelPartitionsInVisitOrder() {
        return List.of(expectedName(CHUNKS_NEW, CODE_3, null), expectedName(CHUNKS_NEW, BGE_768, null)).stream()
            .sorted().toList();
    }

    private static String modelOf(String modelPartition) {
        return modelPartition.equals(expectedName(CHUNKS_NEW, CODE_3, null)) ? CODE_3 : BGE_768;
    }

    /**
     * Two model partitions on both scratch parents; one existing tenant with a collection and a chunk under each
     * model, so foreign-key checks on the referencing tables are real.
     */
    private void seedExistingTenant() throws Exception {
        twoModelsOnBothParents();
        try (Connection a = admin()) {
            PgContainerHelper.seedServiceToken(dsl(a), "tok-existing-p225", "existing", "p225");
            PartitionScratch.createTenantPartitions(dsl(a), CHUNKS_NEW, "existing", true);
            PartitionScratch.createTenantPartitions(dsl(a), CENTROIDS_NEW, "existing", true);
        }
        try (Connection c = svc()) {
            c.setAutoCommit(true);
            PgContainerHelper.setTenant(c, "nexus.tenant", "existing", false);
            PgContainerHelper.insertCollection(dsl(c), "existing", CODE_COLL);
            PgContainerHelper.insertCollection(dsl(c), "existing", BGE_COLL);
            PartitionScratch.insertChunk(dsl(c), "existing", CODE_COLL, new byte[32], CODE_3, 1024);
            PartitionScratch.insertChunk(dsl(c), "existing", BGE_COLL, new byte[32], BGE_768, 768);
            // One more chunk per model for each round of the pending-creation scenarios to reference.
            for (int k = 1; k <= 5; k++) {
                PartitionScratch.insertChunk(dsl(c), "existing", CODE_COLL, hash(200 + k), CODE_3, 1024);
                PartitionScratch.insertChunk(dsl(c), "existing", BGE_COLL, hash(200 + k), BGE_768, 768);
            }
        }
    }

    @Test
    void theLocksACreationTakes_areRecorded() throws Exception {
        seedExistingTenant();
        try (Connection c = svc()) {
            c.setAutoCommit(false);
            var ctx = dsl(c);
            int pid = PartitionScratch.backendPid(ctx);
            PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "locks-tenant", true);
            var locks = PartitionScratch.locks(ctx, pid);
            c.rollback();
            PartitionScratch.evidence("LOCKS held after create_tenant_partitions(chunks_new, new tenant), 2 model partitions, before commit:");
            locks.forEach(l -> PartitionScratch.evidence("  " + l));
            for (String mp : modelPartitionsInVisitOrder()) {
                assertThat(locks.stream().filter(l -> l.relation().equals(mp)).map(PartitionScratch.LockRow::mode))
                    .as(mp).contains("AccessExclusiveLock");
            }
            for (String referenced : List.of("catalog_collections", "t225_ref_manifest", "t225_ref_assign", "t225_ref_orphan")) {
                assertThat(locks.stream().filter(l -> l.relation().equals(referenced)).map(PartitionScratch.LockRow::mode))
                    .as(referenced).contains("ShareRowExclusiveLock");
            }
            // Nothing is taken on the root parent itself.
            assertThat(locks).extracting(PartitionScratch.LockRow::relation).doesNotContain(CHUNKS_NEW);
        }
    }

    @Test
    void aCreationBehindAnOpenReaderFailsAtTheLockTimeout() throws Exception {
        seedExistingTenant();
        String mp = modelPartitionsInVisitOrder().get(0);
        try (Connection reader = admin(); Connection c = svc()) {
            reader.setAutoCommit(false);
            dsl(reader).selectCount().from(DSL.table(DSL.name("nexus", mp))).fetch();   // ACCESS SHARE on the model partition, held open
            var ctx = dsl(c);
            long t0 = System.nanoTime();
            assertThatThrownBy(() -> PartitionScratch.createTenantPartitions(ctx, CHUNKS_NEW, "blocked", true))
                .satisfies(t -> assertThat(sqlState(t)).isEqualTo("55P03"));
            long ms = (System.nanoTime() - t0) / 1_000_000;
            PartitionScratch.evidence("TIMEOUT creation behind an open reader on the model partition failed with 55P03 after " + ms + " ms (lock_timeout 2s)");
            assertThat(ms).isBetween(1800L, 4500L);
            reader.rollback();
        }
    }

    /** One blocker connection per relation a creation may wait for; each holds a lock that conflicts with the creation's. */
    private Connection blockerFor(String relation, String firstMp) throws Exception {
        if (relation.equals(firstMp)) {
            Connection r = admin();
            r.setAutoCommit(false);
            dsl(r).selectCount().from(DSL.table(DSL.name("nexus", relation))).fetch();   // ACCESS SHARE, held open
            return r;
        }
        Connection w = svc();
        w.setAutoCommit(false);
        PgContainerHelper.setTenant(w, "nexus.tenant", "existing", true);
        var tenantCol = DSL.field(DSL.name("tenant_id"), String.class);
        // An UPDATE that matches no row: ROW EXCLUSIVE on the table, no foreign-key check that would lock the chunk tree.
        dsl(w).update(DSL.table(DSL.name("nexus", relation))).set(tenantCol, tenantCol).where(DSL.falseCondition()).execute();
        return w;
    }

    /**
     * With the creation parked on {@code blocked} (the acquisitions before it already granted), how long does
     * each kind of writer wait? Prediction written before the run, from the acquisition order the staggered test
     * shows (first model partition, registry, manifest, assign, orphan, then the second model partition): a writer
     * waits when it conflicts with a lock the creation HOLDS or queues behind the lock it is WAITING for, and
     * nothing else waits. The second model partition is reached last, so its writers never wait.
     */
    private Map<String, Long> writerWaitsWhileCreationIsParkedOn(String blocked, int round) throws Exception {
        List<String> order = modelPartitionsInVisitOrder();
        String firstMp = order.get(0);
        String firstModel = modelOf(firstMp);
        String secondModel = modelOf(order.get(1));
        String secondColl = secondModel.equals(CODE_3) ? CODE_COLL : BGE_COLL;
        int secondDim = secondModel.equals(CODE_3) ? 1024 : 768;
        Map<String, Long> waits = new LinkedHashMap<>();
        try (Connection blocker = blockerFor(blocked, firstMp); Connection creator = svc()) {
            int creatorPid = PartitionScratch.backendPid(dsl(creator));
            AtomicLong creatorStart = new AtomicLong();
            AtomicLong creatorEnd = new AtomicLong();
            CompletableFuture<Void> creation = CompletableFuture.runAsync(() -> {
                creatorStart.set(System.nanoTime());
                try {
                    PartitionScratch.createTenantPartitions(dsl(creator), CHUNKS_NEW, "pending" + round, true);
                } catch (RuntimeException expected) {
                    // 55P03 at the timeout
                } finally {
                    creatorEnd.set(System.nanoTime());
                }
            });
            try (Connection probe = admin()) {
                long deadline = System.nanoTime() + 5_000_000_000L;
                while (System.nanoTime() < deadline) {
                    var w = PartitionScratch.waitingLock(dsl(probe), creatorPid);
                    if (w != null && w.relation().equals(blocked)) break;
                    Thread.sleep(10);
                }
                var held = PartitionScratch.waitingLock(dsl(probe), creatorPid);
                assertThat(held).as("creation parked").isNotNull();
                assertThat(held.relation()).isEqualTo(blocked);
                PartitionScratch.evidence("PARKED creation holds " + PartitionScratch.locks(dsl(probe), creatorPid).stream()
                    .filter(l -> l.granted()).map(l -> l.relation() + ":" + l.mode()).toList() + " and waits for " + held);
            }
            List<CompletableFuture<Void>> writers = new ArrayList<>();
            writers.add(timed("model partition the creation reaches first", waits, () ->
                insertChunkAs("existing", firstModel.equals(CODE_3) ? CODE_COLL : BGE_COLL, hash(100 + round * 10),
                    firstModel, firstModel.equals(CODE_3) ? 1024 : 768)));
            writers.add(timed("model partition the creation reaches last", waits, () ->
                insertChunkAs("existing", secondColl, hash(101 + round * 10), secondModel, secondDim)));
            writers.add(timed("registry (insert a collection)", waits, () -> {
                try (Connection w = svc()) {
                    w.setAutoCommit(true);
                    PgContainerHelper.setTenant(w, "nexus.tenant", "existing", false);
                    PgContainerHelper.insertCollection(dsl(w), "existing", "code__p225r" + round + "__voyage-code-3__v1");
                }
            }));
            // Referencing-table writers reference a chunk in the LAST model partition, so a wait is the table's, not the chunk tree's.
            for (String ref : List.of("t225_ref_manifest", "t225_ref_assign", "t225_ref_orphan")) {
                writers.add(timed("referencing table " + ref, waits, () -> {
                    try (Connection w = svc()) {
                        w.setAutoCommit(true);
                        PgContainerHelper.setTenant(w, "nexus.tenant", "existing", false);
                        var ins = dsl(w).insertInto(DSL.table(DSL.name("nexus", ref)))
                            .set(DSL.field(DSL.name("tenant_id"), String.class), "existing")
                            .set(DSL.field(DSL.name("collection"), String.class), secondColl)
                            .set(DSL.field(DSL.name("chash"), byte[].class), hash(200 + round))
                            .set(DSL.field(DSL.name("embedding_model"), String.class), secondModel);
                        if (ref.equals("t225_ref_manifest")) {
                            ins = ins.set(DSL.field(DSL.name("doc_id"), String.class), "d" + round)
                                .set(DSL.field(DSL.name("position"), Integer.class), round);
                        } else if (ref.equals("t225_ref_assign")) {
                            ins = ins.set(DSL.field(DSL.name("doc_id"), String.class), "d" + round)
                                .set(DSL.field(DSL.name("topic_id"), Long.class), (long) round);
                        }
                        ins.execute();
                    }
                }));
            }
            for (var f : writers) f.get(15, TimeUnit.SECONDS);
            creation.get(15, TimeUnit.SECONDS);
            PartitionScratch.evidence("PARKED on " + blocked + ": creation ended after "
                + (creatorEnd.get() - creatorStart.get()) / 1_000_000 + " ms (55P03)");
            blocker.rollback();
        }
        return waits;
    }

    @Test
    void whatOtherWritersWaitBehindAPendingCreation() throws Exception {
        seedExistingTenant();
        String firstMp = modelPartitionsInVisitOrder().get(0);
        // blocked relation -> the writer kinds expected to wait (> 1 s); every other kind is expected to be free (< 1 s)
        Map<String, List<String>> predicted = new LinkedHashMap<>();
        predicted.put(firstMp, List.of("model partition the creation reaches first"));
        predicted.put("catalog_collections", List.of("model partition the creation reaches first", "registry (insert a collection)"));
        predicted.put("t225_ref_manifest", List.of("model partition the creation reaches first", "registry (insert a collection)",
            "referencing table t225_ref_manifest"));
        predicted.put("t225_ref_assign", List.of("model partition the creation reaches first", "registry (insert a collection)",
            "referencing table t225_ref_manifest", "referencing table t225_ref_assign"));
        predicted.put("t225_ref_orphan", List.of("model partition the creation reaches first", "registry (insert a collection)",
            "referencing table t225_ref_manifest", "referencing table t225_ref_assign", "referencing table t225_ref_orphan"));
        int round = 0;
        for (var e : predicted.entrySet()) {
            round++;
            Map<String, Long> waits = writerWaitsWhileCreationIsParkedOn(e.getKey(), round);
            waits.forEach((k, v) -> PartitionScratch.evidence("WRITER wait, creation parked on " + e.getKey() + ": " + k + " = " + v + " ms"));
            waits.forEach((k, v) -> {
                if (e.getValue().contains(k)) {
                    assertThat(v).as("parked on %s, %s", e.getKey(), k).isBetween(1000L, LOCK_TIMEOUT_MS + 2500);
                } else {
                    assertThat(v).as("parked on %s, %s", e.getKey(), k).isLessThan(1000L);
                }
            });
        }
    }

    /** Result of a creation run against staggered blockers. */
    private record Staggered(String outcome, long totalMs, List<String> order) {}

    /**
     * Run {@code create_tenant_partitions(chunks_new, tenant)} against one blocker per relation it may have to
     * wait for (an open reader on the first model partition it reaches; an open writer on the registry and on each
     * referencing table). A coordinator releases each blocker 1.4 s after the creation starts waiting for it, so
     * no single wait reaches the 2 s lock timeout. {@code statementTimeoutMs} (null for none) is set on the
     * creator's session first.
     */
    private Staggered runStaggered(String tenant, Integer statementTimeoutMs) throws Exception {
        String firstMp = modelPartitionsInVisitOrder().get(0);
        Map<String, Connection> blockers = new LinkedHashMap<>();
        try (Connection creator = svc()) {
            Connection r = admin();
            r.setAutoCommit(false);
            dsl(r).selectCount().from(DSL.table(DSL.name("nexus", firstMp))).fetch();
            blockers.put(firstMp, r);
            // The UPDATE matches no row: it takes the table lock without a foreign-key check that would also lock
            // the chunk tree.
            for (String ref : List.of("t225_ref_manifest", "t225_ref_assign", "t225_ref_orphan", "catalog_collections")) {
                Connection w = svc();
                w.setAutoCommit(false);
                PgContainerHelper.setTenant(w, "nexus.tenant", "existing", true);
                var tenantCol = DSL.field(DSL.name("tenant_id"), String.class);
                dsl(w).update(DSL.table(DSL.name("nexus", ref))).set(tenantCol, tenantCol).where(DSL.falseCondition()).execute();
                blockers.put(ref, w);
            }
            if (statementTimeoutMs != null) {
                dsl(creator).select(DSL.function("set_config", String.class, DSL.inline("statement_timeout"),
                    DSL.inline(statementTimeoutMs + "ms"), DSL.inline(false))).fetch();
            }
            int creatorPid = PartitionScratch.backendPid(dsl(creator));
            long t0 = System.nanoTime();
            AtomicLong endedAt = new AtomicLong();
            CompletableFuture<Integer> creation = CompletableFuture.supplyAsync(() -> {
                try {
                    return PartitionScratch.createTenantPartitions(dsl(creator), CHUNKS_NEW, tenant, true);
                } finally {
                    endedAt.set(System.nanoTime());
                }
            });
            List<String> order = new ArrayList<>();
            try (Connection probe = admin()) {
                while (!creation.isDone() && (System.nanoTime() - t0) < 40_000_000_000L) {
                    var w = PartitionScratch.waitingLock(dsl(probe), creatorPid);
                    if (w != null && blockers.containsKey(w.relation()) && !order.contains(w.relation())) {
                        order.add(w.relation());
                        PartitionScratch.evidence("STAGGERED: creation waits for " + w + " at "
                            + (System.nanoTime() - t0) / 1_000_000 + " ms");
                        Thread.sleep(1400);
                        if (!creation.isDone()) blockers.get(w.relation()).rollback();
                    } else {
                        Thread.sleep(25);
                    }
                }
            }
            String outcome;
            try {
                outcome = "created " + creation.get(20, TimeUnit.SECONDS);
            } catch (ExecutionException e) {
                outcome = "failed " + sqlState(e);
            }
            long totalMs = (endedAt.get() - t0) / 1_000_000;
            return new Staggered(outcome, totalMs, order);
        } finally {
            for (Connection b : blockers.values()) {
                try { b.rollback(); b.close(); } catch (SQLException ignored) { }
            }
        }
    }

    @Test
    void lockTimeoutBoundsEachAcquisition_notTheWholeCreation() throws Exception {
        seedExistingTenant();
        var res = runStaggered("staggered", null);
        PartitionScratch.evidence("STAGGERED (no statement_timeout): creation of 2 leaves -> " + res.outcome() + " after "
            + res.totalMs() + " ms across " + res.order().size() + " blocked acquisitions: " + res.order());
        assertThat(res.outcome()).isEqualTo("created 2");
        assertThat(res.order()).as("acquisitions the creation had to wait for").hasSizeGreaterThanOrEqualTo(5);
        assertThat(res.totalMs()).as("total creation time exceeds the 2 s per-acquisition timeout").isGreaterThan(LOCK_TIMEOUT_MS);
    }

    @Test
    void aCallerStatementTimeout_boundsTheWholeCreation() throws Exception {
        seedExistingTenant();
        var res = runStaggered("bounded", 3000);
        PartitionScratch.evidence("STAGGERED (caller statement_timeout 3000 ms): creation -> " + res.outcome() + " after "
            + res.totalMs() + " ms; waited for: " + res.order());
        assertThat(res.outcome()).isEqualTo("failed 57014");
        assertThat(res.totalMs()).isBetween(2800L, 5000L);
    }

    @Test
    void aStatementTimeoutSetInsideAFunction_doesNotShortenTheRunningStatement() throws Exception {
        try (Connection a = admin()) {
            PgContainerHelper.runSuperuserDdl(a,
                "CREATE FUNCTION nexus.t225_sleepy() RETURNS integer LANGUAGE plpgsql SET statement_timeout = '300ms' "
                    + "AS $fn$ BEGIN PERFORM pg_catalog.pg_sleep(1.2); RETURN 1; END $fn$");
            var ctx = dsl(a);
            long t0 = System.nanoTime();
            String outcome;
            try {
                ctx.select(DSL.function(DSL.name("nexus", "t225_sleepy"), Integer.class)).fetch();
                outcome = "returned";
            } catch (DataAccessException e) {
                outcome = "failed " + sqlState(e);
            }
            long ms = (System.nanoTime() - t0) / 1_000_000;
            PartitionScratch.evidence("STATEMENT_TIMEOUT function with SET statement_timeout='300ms' sleeping 1200 ms: " + outcome + " after " + ms + " ms");
            assertThat(outcome).as("a function-level statement_timeout does not re-arm the running statement").isEqualTo("returned");
            assertThat(ms).isGreaterThanOrEqualTo(1100L);
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static byte[] hash(int n) {
        byte[] b = new byte[32];
        b[0] = (byte) n;
        b[31] = (byte) (n + 1);
        return b;
    }

    private CompletableFuture<Void> timed(String label, Map<String, Long> out, ThrowingRunnable body) {
        return CompletableFuture.runAsync(() -> {
            long t0 = System.nanoTime();
            try {
                body.run();
            } catch (Exception e) {
                throw new IllegalStateException(label, e);
            }
            synchronized (out) {
                out.put(label, (System.nanoTime() - t0) / 1_000_000);
            }
        });
    }

    private void insertChunkAs(String tenant, String collection, byte[] chash, String model, int dim) throws Exception {
        try (Connection w = svc()) {
            w.setAutoCommit(true);
            PgContainerHelper.setTenant(w, "nexus.tenant", tenant, false);
            PartitionScratch.insertChunk(dsl(w), tenant, collection, chash, model, dim);
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }
}
