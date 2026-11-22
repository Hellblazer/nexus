// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-afkjy: {@link CollectionRegistry#require} answers a cached KNOWN entry with no read, so
 * {@link CatalogRepository#upsertCollection} must evict the name once its transaction commits, or a content type the
 * upsert changed stays invisible until the process restarts. Each test plants its own tenant.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRegistryUpsertEvictTest {

    private static final String SVC_ROLE = "svc_registry_upsert_evict_test";
    private static final String SVC_PASS = "svc_registry_upsert_evict_test_pass";
    private static final AtomicInteger SEQ = new AtomicInteger();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
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
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String newTenant(String tag) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "afkjy-" + tag + "-" + n;
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES),
                "afkjy-token-" + tag + "-" + n, tenant, "afkjy-test");
        }
        return tenant;
    }

    private void upsert(String tenant, String name, String contentType) {
        Map<String, Object> coll = new HashMap<>();
        coll.put("name", name);
        coll.put("content_type", contentType);
        coll.put("model_version", "v1");
        // A fresh tenant has no embedding_profile row, so a new row must name its model (as LifecycleFromRowTest does).
        coll.put("embedding_model", "minilm-l6-v2-384");
        repo.upsertCollection(tenant, coll);
    }

    private String requiredContentType(String tenant, String name) throws Exception {
        return tenantScope.withTenant(tenant, ctx -> CollectionRegistry.require(ctx, tenant, name)).contentType();
    }

    @Test
    void anUpsertThatChangesContentTypeIsSeenByTheNextRequire_withNoRestart() throws Exception {
        String tenant = newTenant("retype");
        String name = "afkjy-retype-" + SEQ.incrementAndGet();
        upsert(tenant, name, "knowledge");
        assertThat(requiredContentType(tenant, name)).isEqualTo("knowledge");
        assertThat(CollectionRegistry.cached(tenant, name)).as("require warmed the cache").isPresent();

        upsert(tenant, name, "docs");

        assertThat(CollectionRegistry.cached(tenant, name)).as("the upsert evicted the stale entry").isEmpty();
        assertThat(requiredContentType(tenant, name)).isEqualTo("docs");
    }

    @Test
    void anUpsertOfAnotherNameLeavesThisNamesCachedEntry() throws Exception {
        String tenant = newTenant("other");
        String kept = "afkjy-kept-" + SEQ.incrementAndGet();
        String other = "afkjy-other-" + SEQ.incrementAndGet();
        upsert(tenant, kept, "knowledge");
        assertThat(requiredContentType(tenant, kept)).isEqualTo("knowledge");

        upsert(tenant, other, "docs");

        assertThat(CollectionRegistry.cached(tenant, kept)).as("only the upserted name is evicted").isPresent();
    }
}
