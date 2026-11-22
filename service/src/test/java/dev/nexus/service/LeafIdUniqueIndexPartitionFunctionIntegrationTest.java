// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.IdKeyIndexProbe.Idx;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.3b (nexus-p2m1x.20), the leaf helper's half: from the cut on, a leaf created through
 * {@code nexus.partition_ensure_leaf} (which {@code nexus.create_tenant_partitions}, run by the service_tokens trigger, calls for
 * every model partition) carries the VALID unique id index, so a leaf added after the pass never lacks one (RDR item 6, "Index
 * builds follow the PCI reconciler's pattern"; T2 nexus_rdr/229-research-51). The function cannot use
 * {@code CREATE INDEX CONCURRENTLY} (it runs in a transaction); the new leaf is empty, so a plain {@code CREATE UNIQUE INDEX} is
 * the bead's route. The keys are {@link IdKeyIndexProbe}'s, found by what they index and never by name.
 *
 * <p>Beside {@link TenantPartitionFunctionsIntegrationTest}, which tests these functions on scratch parents; this class runs
 * them on the live parents of an owner-migrated database (production's nexus_admin shape), because the claim is about the
 * live tables' leaves. The two columns the key names exist since P3.1 (vectors-068); no fixture adds one.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LeafIdUniqueIndexPartitionFunctionIntegrationTest {

    private static final String ADMIN_ROLE = PgContainerHelper.TEMPLATE_OWNER;
    private static final String ADMIN_PASS = PgContainerHelper.TEMPLATE_OWNER_PASSWORD;

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;
    final List<String> tenants = new ArrayList<>();

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.startMigratedByOwner();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        adminDs = new HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void startClean() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            IdKeyIndexProbe.reset(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    @AfterEach
    @SuppressWarnings("deprecation")
    void endClean() throws Exception {
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            DSLContext ctx = DSL.using(owner, SQLDialect.POSTGRES);
            for (String tenant : tenants) {
                dev.nexus.service.jooq.nexus.Routines.dropTenantPartitions(ctx.configuration(), tenant);
            }
        }
        tenants.clear();
    }

    private <T> T asOwner(java.util.function.Function<DSLContext, T> work) throws Exception {
        try (Connection owner = adminDs.getConnection()) {
            owner.setAutoCommit(true);
            return work.apply(DSL.using(owner, SQLDialect.POSTGRES));
        }
    }

    /** The unique id indexes of the tenant's leaves of {@code key}'s table (a leaf's relation name ends with the tenant's hash). */
    private List<Idx> leafIndexes(IdKeyIndexProbe.Key key, List<String> leaves) throws Exception {
        return asOwner(ctx -> IdKeyIndexProbe.indexes(ctx, key)).stream().filter(i -> i.leaf() && leaves.contains(i.relation())).toList();
    }

    private List<String> tenantLeaves(IdKeyIndexProbe.Key key, String tenant) throws Exception {
        String suffix = "_t_" + hash16(tenant);
        return asOwner(ctx -> IdKeyIndexProbe.leaves(ctx, key)).stream().filter(l -> l.endsWith(suffix)).toList();
    }

    private static String hash16(String value) {
        try {
            byte[] d = java.security.MessageDigest.getInstance("SHA-256").digest(value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(d).substring(0, 16);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * The production path: {@code nexus.create_tenant_partitions} creates the tenant's leaf under every model partition of
     * both tables, through {@code partition_ensure_leaf}. Each leaf has exactly one unique id index and it is valid.
     */
    @Test
    @SuppressWarnings("deprecation")
    void aLeafCreatedAfterTheCutThroughCreateTenantPartitionsHasTheValidUniqueIndex() throws Exception {
        String tenant = "p33b-leaf-" + System.nanoTime();
        tenants.add(tenant);
        asOwner(ctx -> {
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(ctx.configuration(), "nexus.chunks", tenant, true, true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(ctx.configuration(), "nexus.taxonomy_centroids", tenant, true, true);
            return null;
        });

        for (var key : IdKeyIndexProbe.KEYS) {
            List<String> leaves = tenantLeaves(key, tenant);
            assertThat(leaves).as("%s: the tenant has a leaf under each model partition, two at least", key.table())
                .hasSizeGreaterThanOrEqualTo(2);
            List<Idx> indexes = leafIndexes(key, leaves);
            assertThat(indexes.stream().map(Idx::relation).toList()).as("%s: one unique id index on every new leaf", key.table())
                .containsExactlyInAnyOrderElementsOf(leaves);
            assertThat(indexes).as("%s: valid", key.table()).allMatch(Idx::valid);
        }
    }

    /**
     * The helper itself, called directly on one model partition of each table for a tenant that has no leaf there yet
     * ({@code p_force} true, the owner's call): the leaf it returns true for has the valid unique index. A second call finds
     * the leaf by its bound, returns false and adds no second index.
     */
    @Test
    void partitionEnsureLeafCalledDirectlyIndexesTheLeafItCreatesAndOnlyThat() throws Exception {
        String tenant = "p33b-ensure-" + System.nanoTime();
        tenants.add(tenant);
        for (var key : IdKeyIndexProbe.KEYS) {
            String parent = "nexus." + key.table();
            String modelPartition = asOwner(ctx -> IdKeyIndexProbe.modelPartitions(ctx, key)).get(0);
            boolean created = asOwner(ctx -> ctx.fetchValue(
                "SELECT nexus.partition_ensure_leaf(?::regclass, ?::regclass, ?, true)", parent, modelPartition, tenant)
                instanceof Boolean b && b);
            assertThat(created).as("%s: the leaf did not exist, so the helper creates it", key.table()).isTrue();

            List<String> leaves = tenantLeaves(key, tenant);
            assertThat(leaves).as("%s: exactly the one leaf it created", key.table()).hasSize(1);
            assertThat(leafIndexes(key, leaves)).as("%s: it has one unique id index, valid", key.table())
                .hasSize(1).allMatch(Idx::valid);

            boolean again = asOwner(ctx -> ctx.fetchValue(
                "SELECT nexus.partition_ensure_leaf(?::regclass, ?::regclass, ?, true)", parent, modelPartition, tenant)
                instanceof Boolean b && b);
            assertThat(again).as("%s: the second call finds the leaf by its bound", key.table()).isFalse();
            assertThat(leafIndexes(key, leaves)).as("%s: and adds no second index", key.table()).hasSize(1);
        }
    }

    /**
     * The state after the pass (linda's seam ruling 2026-11-22, item 3): the parent carries the id UNIQUE constraint. The helper then
     * branches on that constraint in pg_constraint, not on an index name: PostgreSQL itself creates the index on a leaf made under a
     * constrained parent (under a name of its own), so the helper must create none, and the leaf ends with exactly one unique id
     * index and no duplicate. The fixture adds the constraint the way PostgreSQL's recursive form does (every existing partition at
     * both levels gets and attaches its own), which is the end state of the pass's route; both creation paths are run on a new
     * tenant, and both before-the-constraint tests above stay.
     *
     * <p>Against the helper as it is on this branch (it creates no index) this test is green: PostgreSQL's index is the one index.
     * It is the guard for the change that makes the helper index a leaf in the pre-constraint state, which must not do it here too.
     */
    @Test
    @SuppressWarnings("deprecation")
    void withTheParentConstraintPresentANewLeafEndsWithExactlyOneUniqueIdIndex() throws Exception {
        for (var key : IdKeyIndexProbe.KEYS) {
            asOwner(ctx -> ctx.execute("ALTER TABLE nexus." + key.table() + " ADD CONSTRAINT p33b_fixture_" + key.table()
                + "_cid_key UNIQUE (" + key.columns() + ")"));
        }
        String viaCreate = "p33b-post-create-" + System.nanoTime();
        String viaEnsure = "p33b-post-ensure-" + System.nanoTime();
        tenants.add(viaCreate);
        tenants.add(viaEnsure);
        asOwner(ctx -> {
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(ctx.configuration(), "nexus.chunks", viaCreate, true, true);
            dev.nexus.service.jooq.nexus.Routines.createTenantPartitions(ctx.configuration(), "nexus.taxonomy_centroids", viaCreate, true, true);
            return null;
        });
        for (var key : IdKeyIndexProbe.KEYS) {
            String modelPartition = asOwner(ctx -> IdKeyIndexProbe.modelPartitions(ctx, key)).get(0);
            boolean created = asOwner(ctx -> ctx.fetchValue(
                "SELECT nexus.partition_ensure_leaf(?::regclass, ?::regclass, ?, true)", "nexus." + key.table(), modelPartition, viaEnsure)
                instanceof Boolean b && b);
            assertThat(created).as("%s: the helper created the leaf", key.table()).isTrue();

            assertThat(asOwner(ctx -> IdKeyIndexProbe.indexes(ctx, key)).stream().filter(i -> i.level() == 0).toList())
                .as("%s: precondition: the parent carries the id constraint", key.table()).hasSize(1);
            for (String tenant : List.of(viaCreate, viaEnsure)) {
                List<String> leaves = tenantLeaves(key, tenant);
                assertThat(leaves).as("%s %s: leaves", key.table(), tenant).isNotEmpty();
                assertThat(leafIndexes(key, leaves).stream().map(Idx::relation).toList())
                    .as("%s %s: exactly one unique id index on every new leaf, no duplicate", key.table(), tenant)
                    .containsExactlyInAnyOrderElementsOf(leaves);
                assertThat(leafIndexes(key, leaves)).as("%s %s: valid and attached to the model partition's", key.table(), tenant)
                    .allMatch(i -> i.valid() && i.parentIndex() != null);
            }
        }
    }
}
