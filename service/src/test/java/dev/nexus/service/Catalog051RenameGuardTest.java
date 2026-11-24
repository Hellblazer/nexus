// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.MethodOrderer;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HashMap;
import java.util.Map;

/**
 * RDR-229 (bead nexus-p2m1x.14), catalog-051-2 meets rename: renaming a legacy slug collection whose documents name an
 * owner that a THIRD live collection already holds (same content type, model and version) keeps the new row's slug and
 * raises no error, whether or not the partial UNIQUE index on the tuple exists. The source is not the holder here, so the
 * rename-side re-derivation (renameCollectionTxn, after the source is retired) finds the tuple still held and the guard
 * skips the rewrite.
 *
 * <p>The sibling case, where the SOURCE is the only holder, is
 * {@code CatalogRepositoryTest#collectionOwner_renamedRepoCollectionTakesTheDocumentsOwner}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class Catalog051RenameGuardTest {

    private static final String SVC_ROLE = "svc_catalog049r_test";
    private static final String SVC_PASS = "svc_catalog049r_test_pass";
    private static final String MODEL = "voyage-code-3";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private CatalogRepository repo;

    @BeforeAll
    void start() throws Exception {
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
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test @Order(1)
    void renameOfASlugCollectionWhoseDestinationTupleAThirdCollectionHolds_keepsTheSlug_withoutTheIndex() {
        renameKeepsTheSlug("cat049r-noindex", "1.41.1", "1-41", "41");
    }

    /** Order 2: the index is created here and stays for the rest of the class, so the no-index case runs first. */
    @Test @Order(2)
    void renameOfASlugCollectionWhoseDestinationTupleAThirdCollectionHolds_keepsTheSlug_withTheIndex() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Catalog051OwnerGuardTest.createLiveTupleIndex(org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES));
        }
        renameKeepsTheSlug("cat049r-index", "1.42.1", "1-42", "42");
    }

    private void renameKeepsTheSlug(String tenant, String tumbler, String owner, String tag) {
        String holder = "code__" + owner + "__voyage-code-3__v1";
        String src = "code__old-" + tag + "aa00bb__voyage-code-3__v1";
        String neu = "code__new-" + tag + "cc22dd__voyage-code-3__v1";
        register(tenant, holder, owner);
        register(tenant, src, "old-" + tag + "aa00bb");
        putDoc(tenant, tumbler, src);
        assertThat(ownerOf(tenant, src)).as("guard: the third collection holds the tuple, so the source keeps its slug")
            .isEqualTo("old-" + tag + "aa00bb");

        repo.renameCollection(tenant, src, neu, null, "code", "new-" + tag + "cc22dd");

        assertThat(ownerOf(tenant, neu)).as("the renamed row keeps its slug: the third collection still holds %s", owner)
            .isEqualTo("new-" + tag + "cc22dd");
        assertThat(ownerOf(tenant, holder)).isEqualTo(owner);
        assertThat(repo.getCollection(tenant, src).get("superseded_by")).as("the source was retired").isEqualTo(neu);
    }

    private void register(String tenant, String name, String owner) {
        repo.upsertCollection(tenant, Map.of(
            "name", name, "content_type", "code", "owner_id", owner,
            "embedding_model", MODEL, "model_version", "v1", "lifecycle_state", "live"));
    }

    private void putDoc(String tenant, String tumbler, String collection) {
        Map<String, Object> doc = new HashMap<>();
        doc.put("tumbler", tumbler);
        doc.put("title", "doc " + tumbler);
        doc.put("content_type", "code");
        doc.put("physical_collection", collection);
        doc.put("source_uri", "file:///cat049r/" + tenant + "/" + tumbler);
        repo.upsertDocument(tenant, doc);
    }

    private String ownerOf(String tenant, String name) {
        return (String) repo.getCollection(tenant, name).get("owner_id");
    }
}
