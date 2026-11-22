// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
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
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.catchThrowable;

/**
 * nexus-afkjy: a document move compares the two collections' owners, and owner_id can change behind the
 * {@link CollectionRegistry} cache with no evict: catalog-044's AFTER trigger on {@code catalog_documents} re-owns a
 * slug-owned code/docs/rdr row when a document lands. {@link CombinedWriteService#moveDocsCombined} must read the owners
 * from the rows, so a move between collections the trigger has just made siblings is not refused on a stale owner.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CombinedWriteOwnerFreshReadTest {

    private static final String SVC_ROLE = "svc_combined_owner_fresh_test";
    private static final String SVC_PASS = "svc_combined_owner_fresh_test_pass";
    private static final String TENANT = "afkjy-owner-fresh";
    private static final String SRC = "docs-src-afkjy";
    private static final String TGT = "docs-tgt-afkjy";
    private static final String SRC_MODEL = "voyage-context-3";
    private static final String TGT_MODEL = "minilm-l6-v2-384";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CombinedWriteService service;

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
        // No chunks are moved, so nothing is embedded and no router is needed.
        service = new CombinedWriteService(tenantScope, new CatalogRepository(tenantScope), null);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void su(java.util.function.Consumer<DSLContext> body) throws Exception {
        try (Connection c = pg.createConnection("")) {
            c.setAutoCommit(true);
            body.accept(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private static void collection(DSLContext ctx, String name, String owner, String model) {
        ctx.insertInto(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.TENANT_ID, TENANT).set(CATALOG_COLLECTIONS.NAME, name)
            .set(CATALOG_COLLECTIONS.CONTENT_TYPE, "docs").set(CATALOG_COLLECTIONS.OWNER_ID, owner)
            .set(CATALOG_COLLECTIONS.EMBEDDING_MODEL, model).set(CATALOG_COLLECTIONS.MODEL_VERSION, "v1")
            .set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "live").set(CATALOG_COLLECTIONS.SUPERSEDED_BY, "")
            .execute();
    }

    private String ownerOf(String name) throws Exception {
        String[] out = new String[1];
        su(ctx -> out[0] = ctx.select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID));
        return out[0];
    }

    private void move() {
        service.moveDocsCombined(TENANT, SRC, TGT, List.of(), List.of(), Map.of());
    }

    @Test
    void aMoveSeesTheOwnerTheDocumentTriggerJustGaveTheSource_notTheCachedOne() throws Exception {
        su(ctx -> {
            PgContainerHelper.seedServiceToken(ctx, "afkjy-owner-fresh-token", TENANT, "afkjy-test");
            ctx.insertInto(CATALOG_OWNERS, CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME,
                    CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.NEXT_SEQ)
                .values(TENANT, "1.40", "afkjy-repo", "repo", "", 0L).execute();
            collection(ctx, SRC, "slug-afkjy0001", SRC_MODEL);   // a slug owner, as hygiene-002-1 stamped one
            collection(ctx, TGT, "1-40", TGT_MODEL);
        });

        // Different owners: refused, and the lookup has cached the source's slug owner.
        assertThatThrownBy(this::move).isInstanceOf(IllegalArgumentException.class).hasMessageContaining("model siblings");
        assertThat(CollectionRegistry.cached(TENANT, SRC)).isPresent();

        // A document lands in the source: catalog-044's AFTER trigger re-owns the row to its documents' owner, behind the cache.
        su(ctx -> ctx.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, "1.40.1")
            .set(CATALOG_DOCUMENTS.TITLE, "doc 1.40.1").set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, SRC)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CollectionIds.idOf(TENANT, SRC))
            .execute());
        assertThat(ownerOf(SRC)).as("the trigger re-owned the source").isEqualTo("1-40");
        assertThat(CollectionRegistry.cached(TENANT, SRC).orElseThrow().ownerId())
            .as("the cache still holds the slug: nothing evicted it").isEqualTo("slug-afkjy0001");

        // Now the two are model siblings; the move must not be refused on the cached owner.
        Throwable t = catchThrowable(this::move);
        assertThat(t == null ? "" : String.valueOf(t.getMessage()))
            .as("not refused as non-siblings").doesNotContain("model siblings");
    }
}
