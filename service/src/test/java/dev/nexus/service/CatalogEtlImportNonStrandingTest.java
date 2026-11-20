// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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

/**
 * RDR-229 P2.1 (bead nexus-p2m1x.6): the ETL-import non-stranding guard (item 9, the fourth migration source).
 *
 * <p>The engine's ETL import ({@code POST /v1/catalog/import/collection}: {@link CatalogRepository#importCollection}
 * for one row, {@link CatalogRepository#importCollectionsBatch}, the route's own path) takes rows from another
 * install or an older era. Such a row can carry a legacy two-segment name and no {@code lifecycle_state}: that
 * column did not exist when the row was exported. The import derives the state from the name today
 * ({@code lifecycleStateForImport}, the {@code quarantine-} prefix); item 2 will make it read the imported row's own
 * {@code lifecycle_state} when present and keep the prefix as the fallback. These tests feed row-less legacy names
 * through the real import and pin the fallback, so that change cannot strand a row that carries no
 * {@code lifecycle_state}.
 *
 * <p>Each row lands with the right {@code lifecycle_state}, and with the content_type, owner_id, embedding_model
 * and model_version the row carried (the import trusts the row for those; it parses no name for them). The
 * explicit-{@code lifecycle_state} cases use a value that agrees with what the prefix says, so they hold on
 * develop and after the item 2 change alike; a row whose explicit state disagrees with its name is P2.5's own
 * test, not pinned here.
 *
 * <p>RDR-229 item 9 non-stranding guard: keep until P4.4 retires the fallback.
 *
 * <p>Hermetic embedded Postgres (zonky), the whole Liquibase changelog, the service role under RLS: the setup of
 * {@code CatalogRepositoryTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogEtlImportNonStrandingTest {

    private static final String TENANT = "etl-nonstrand-a";
    private static final String SVC_ROLE = "svc_etl_nonstrand";
    private static final String SVC_PASS = "svc_etl_nonstrand_pass";

    /** A row as another install exported it: no lifecycle_state, no model_version of the era. */
    private static Map<String, Object> legacyRow(String name, String contentType, String owner, String model) {
        return Map.of("name", name, "content_type", contentType, "owner_id", owner,
                      "embedding_model", model, "model_version", "v1");
    }

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** The column, read as the superuser: {@code getCollection} does not project it. */
    private String lifecycleState(String name) {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
                .and(CATALOG_COLLECTIONS.NAME.eq(name))
                .fetchOne(CATALOG_COLLECTIONS.LIFECYCLE_STATE);
        } catch (Exception e) {
            throw new IllegalStateException("reading lifecycle_state of " + name + " failed", e);
        }
    }

    private void assertLanded(String name, String contentType, String owner, String model, String lifecycle) {
        var row = repo.getCollection(TENANT, name);
        assertThat(row).as("guard: the imported row must exist, or every assertion below is vacuous: " + name)
            .isNotNull();
        assertThat(row.get("content_type")).as(name + " content_type").isEqualTo(contentType);
        assertThat(row.get("owner_id")).as(name + " owner_id").isEqualTo(owner);
        assertThat(row.get("embedding_model")).as(name + " embedding_model").isEqualTo(model);
        assertThat(row.get("model_version")).as(name + " model_version").isEqualTo("v1");
        assertThat(lifecycleState(name)).as(name + " lifecycle_state").isEqualTo(lifecycle);
    }

    @Test
    void batchImport_rowlessLegacyNames_deriveLifecycleFromTheNameWhenTheRowCarriesNone() {
        // The route's own path (handleImportCollection -> importCollectionsBatch).
        String quarantineFour = "quarantine-code__nsg-etl-b1__voyage-code-3__v1";
        String quarantineTwo = "quarantine-docs__nsg-etl-b2";
        String plainTwo = "code__nsg-etl-b3";
        String plainFour = "docs__nsg-etl-b4__voyage-context-3__v1";
        int imported = repo.importCollectionsBatch(TENANT, List.of(
            legacyRow(quarantineFour, "code", "nsg-etl-b1", "voyage-code-3"),
            legacyRow(quarantineTwo, "docs", "nsg-etl-b2", "voyage-context-3"),
            legacyRow(plainTwo, "code", "nsg-etl-b3", "voyage-code-3"),
            legacyRow(plainFour, "docs", "nsg-etl-b4", "voyage-context-3")));

        assertThat(imported).as("guard: all four rows were sent").isEqualTo(4);
        assertLanded(quarantineFour, "code", "nsg-etl-b1", "voyage-code-3", "quarantine");
        assertLanded(quarantineTwo, "docs", "nsg-etl-b2", "voyage-context-3", "quarantine");
        assertLanded(plainTwo, "code", "nsg-etl-b3", "voyage-code-3", "live");
        assertLanded(plainFour, "docs", "nsg-etl-b4", "voyage-context-3", "live");
    }

    @Test
    void singleImport_rowlessLegacyNames_deriveLifecycleFromTheNameWhenTheRowCarriesNone() {
        // The single-row delegate (doImportCollection) shares the rule; a quarantine- prefix on the
        // content_type alone, with a plain name, is the rule's other disjunct.
        String quarantineByName = "quarantine-knowledge__nsg-etl-s1";
        String quarantineByType = "knowledge__nsg-etl-s2";
        String plain = "knowledge__nsg-etl-s3";
        repo.importCollection(TENANT, legacyRow(quarantineByName, "knowledge", "nsg-etl-s1", "voyage-context-3"));
        repo.importCollection(TENANT, legacyRow(quarantineByType, "quarantine-knowledge", "nsg-etl-s2", "voyage-context-3"));
        repo.importCollection(TENANT, legacyRow(plain, "knowledge", "nsg-etl-s3", "voyage-context-3"));

        assertLanded(quarantineByName, "knowledge", "nsg-etl-s1", "voyage-context-3", "quarantine");
        assertLanded(quarantineByType, "quarantine-knowledge", "nsg-etl-s2", "voyage-context-3", "quarantine");
        assertLanded(plain, "knowledge", "nsg-etl-s3", "voyage-context-3", "live");
    }

    @Test
    void batchImport_aRowCarryingAnAgreeingLifecycleState_landsWithThatState() {
        // The explicit arm of item 2, on the value that agrees with the name: today the import ignores the
        // field and the prefix answers; after item 2 the field answers; both read the same here.
        String quarantine = "quarantine-code__nsg-etl-e1__voyage-code-3__v1";
        String live = "code__nsg-etl-e2__voyage-code-3__v1";
        var explicitQuarantine = new java.util.HashMap<>(
            legacyRow(quarantine, "code", "nsg-etl-e1", "voyage-code-3"));
        explicitQuarantine.put("lifecycle_state", "quarantine");
        var explicitLive = new java.util.HashMap<>(legacyRow(live, "code", "nsg-etl-e2", "voyage-code-3"));
        explicitLive.put("lifecycle_state", "live");
        repo.importCollectionsBatch(TENANT, List.of(explicitQuarantine, explicitLive));

        assertLanded(quarantine, "code", "nsg-etl-e1", "voyage-code-3", "quarantine");
        assertLanded(live, "code", "nsg-etl-e2", "voyage-code-3", "live");
    }
}
