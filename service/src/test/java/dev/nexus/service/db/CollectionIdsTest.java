// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.1 stage 2a (nexus-p2m1x.17): {@link CollectionIds}, the one way a writer turns {@code (tenant, collection
 * name)} into the registry's {@code collection_id} inside its own transaction.
 *
 * <p>Pins: a registered name gives its id, in both shapes (the jOOQ subselect and the explicit lookup); an unregistered
 * name gives NULL from the subselect and a typed {@link UnregisteredCollectionException} from the lookup; {@code ''}
 * and {@code null} give NULL without a registry read (the empty-string ruling: {@code ''} means unset); the answer is
 * the registry's id AT WRITE TIME from the writer's own transaction, so a row this transaction inserted resolves
 * before it commits, a row it rolled back does not resolve afterwards (no cache survives a transaction), and a
 * re-registered name resolves to its new id; two tenants holding one name get their own ids.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionIdsTest {

    private static final String TENANT = "collection-ids-a";
    private static final String OTHER = "collection-ids-b";
    private static final String COLL = "code__collection-ids__voyage-code-3__v1";
    private static final String COLL2 = "docs__collection-ids__bge-base-en-v15-768__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(3);
        cfg.setConnectionTimeout(15000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void register(String tenant, String name) {
        tenantScope.withTenant(tenant, ctx -> {
            PgContainerHelper.insertCollection(ctx, tenant, name);
            return null;
        });
    }

    private Long registryId(String tenant, String name) {
        return tenantScope.withTenant(tenant, ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    @Test
    void aRegisteredNameGivesItsIdInBothShapes() {
        register(TENANT, COLL);
        register(TENANT, COLL2);
        Long id = registryId(TENANT, COLL);
        Long id2 = registryId(TENANT, COLL2);
        assertThat(id).isNotNull();
        assertThat(id2).isNotNull().isNotEqualTo(id);

        Map<String, Long> got = tenantScope.withTenant(TENANT, ctx -> Map.of(
            "lookup", CollectionIds.require(ctx, TENANT, COLL),
            "lookup2", CollectionIds.require(ctx, TENANT, COLL2),
            "subselect", ctx.select(CollectionIds.idOf(TENANT, COLL)).fetchOne(0, Long.class),
            "subselect2", ctx.select(CollectionIds.idOf(TENANT, COLL2)).fetchOne(0, Long.class)));
        assertThat(got).containsEntry("lookup", id).containsEntry("subselect", id)
            .containsEntry("lookup2", id2).containsEntry("subselect2", id2);
    }

    @Test
    void theSubselectWorksAsAnInsertOrUpdateValue() {
        String successor = "docs__collection-ids-succ__bge-base-en-v15-768__v1";
        register(TENANT, successor);
        Long succId = registryId(TENANT, successor);
        // the shape supersedeCollection needs: the name and the id of the successor written together
        tenantScope.withTenant(TENANT, ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CollectionIds.idOf(TENANT, successor))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL))
            .execute());
        Long stamped = tenantScope.withTenant(TENANT, ctx -> ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL))
            .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID));
        assertThat(stamped).isEqualTo(succId);
        // leave the row as other tests expect it
        tenantScope.withTenant(TENANT, ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "").setNull(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(COLL))
            .execute());
    }

    @Test
    void theFieldShapeResolvesEachRowOfASelectAgainstItsOwnNames() {
        register(TENANT, COLL);
        List<Boolean> agree = tenantScope.withTenant(TENANT, ctx -> {
            var c = CATALOG_COLLECTIONS.as("c");
            return ctx.select(CollectionIds.idOf(c.TENANT_ID, c.NAME).eq(c.COLLECTION_ID))
                .from(c).where(c.TENANT_ID.eq(TENANT)).and(c.NAME.ne("")).fetch(0, Boolean.class);
        });
        assertThat(agree).isNotEmpty().containsOnly(true);
    }

    @Test
    void anUnregisteredNameGivesNullFromTheSubselectAndATypedRefusalFromTheLookup() {
        String missing = "code__collection-ids-missing__voyage-code-3__v1";
        Long viaSubselect = tenantScope.withTenant(TENANT,
            ctx -> ctx.select(CollectionIds.idOf(TENANT, missing)).fetchOne(0, Long.class));
        assertThat(viaSubselect).isNull();

        assertThatThrownBy(() -> tenantScope.withTenant(TENANT, ctx -> CollectionIds.require(ctx, TENANT, missing)))
            .isInstanceOfSatisfying(UnregisteredCollectionException.class, e -> {
                assertThat(e.tenant()).isEqualTo(TENANT);
                assertThat(e.collection()).isEqualTo(missing);
            });
    }

    @Test
    void anEmptyOrNullNameGivesNullWithoutARegistryRead() {
        // even a registry row literally named '' (an unusual but legal name the seeds allow) does not make '' resolve:
        // '' means unset in every CHECK of the expand, so its id column is NULL by rule
        register(TENANT, "");
        Map<String, Long> got = new java.util.HashMap<>();
        tenantScope.withTenant(TENANT, ctx -> {
            got.put("lookup-empty", CollectionIds.require(ctx, TENANT, ""));
            got.put("lookup-null", CollectionIds.require(ctx, TENANT, null));
            got.put("subselect-empty", ctx.select(CollectionIds.idOf(TENANT, "")).fetchOne(0, Long.class));
            got.put("subselect-null", ctx.select(CollectionIds.idOf(TENANT, null)).fetchOne(0, Long.class));
            return null;
        });
        assertThat(got).containsOnlyKeys("lookup-empty", "lookup-null", "subselect-empty", "subselect-null");
        assertThat(got.values()).containsOnlyNulls();
    }

    @Test
    void aRowThisTransactionInsertedResolvesBeforeItCommitsAndNothingSurvivesARollback() {
        String name = "code__collection-ids-rolledback__voyage-code-3__v1";
        Long[] seen = new Long[2];
        assertThatThrownBy(() -> tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, name);
            seen[0] = CollectionIds.require(ctx, TENANT, name);
            seen[1] = ctx.select(CollectionIds.idOf(TENANT, name)).fetchOne(0, Long.class);
            throw new IllegalStateException("roll back");
        })).isInstanceOf(IllegalStateException.class);
        assertThat(seen[0]).isNotNull();
        assertThat(seen[1]).isEqualTo(seen[0]);

        // a cache filled by the lookup above would still answer here; the registry holds no such row, so it must refuse
        assertThatThrownBy(() -> tenantScope.withTenant(TENANT, ctx -> CollectionIds.require(ctx, TENANT, name)))
            .isInstanceOf(UnregisteredCollectionException.class);
    }

    @Test
    void aReRegisteredNameResolvesToItsNewId() {
        String name = "code__collection-ids-reregistered__voyage-code-3__v1";
        register(TENANT, name);
        Long first = tenantScope.withTenant(TENANT, ctx -> CollectionIds.require(ctx, TENANT, name));
        tenantScope.withTenant(TENANT, ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name)).execute());
        register(TENANT, name);
        Long second = tenantScope.withTenant(TENANT, ctx -> CollectionIds.require(ctx, TENANT, name));
        assertThat(second).isNotNull().isNotEqualTo(first).isEqualTo(registryId(TENANT, name));
    }

    @Test
    void twoTenantsHoldingOneNameGetTheirOwnIds() {
        register(TENANT, COLL);
        register(OTHER, COLL);
        Long a = tenantScope.withTenant(TENANT, ctx -> CollectionIds.require(ctx, TENANT, COLL));
        Long b = tenantScope.withTenant(OTHER, ctx -> CollectionIds.require(ctx, OTHER, COLL));
        assertThat(a).isEqualTo(registryId(TENANT, COLL));
        assertThat(b).isEqualTo(registryId(OTHER, COLL)).isNotEqualTo(a);
        // a tenant never resolves a name through another tenant's row
        assertThatThrownBy(() -> tenantScope.withTenant(OTHER, ctx -> CollectionIds.require(ctx, OTHER, COLL2)))
            .isInstanceOf(UnregisteredCollectionException.class);
    }
}
