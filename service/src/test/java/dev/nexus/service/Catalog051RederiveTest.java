// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HashMap;
import java.util.Map;

/**
 * RDR-229 (bead nexus-p2m1x.14), catalog-051-2 and the writes that free a tuple. The guard keeps a slug collection's owner
 * off a tuple a live collection holds; when that holder is superseded or deleted the tuple is free, and the slug row must
 * take the documents' owner then, or it keeps its slug for good. {@code supersedeCollection} re-derives its successor and
 * {@code deleteCollection} re-derives the slug rows the deleted row's tuple held back.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Catalog051RederiveTest {

    private static final String SVC_ROLE = "svc_catalog049d_test";
    private static final String SVC_PASS = "svc_catalog049d_test_pass";
    private static final String MODEL = "voyage-code-3";
    private static final String TENANT = "cat049d-tenant";

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

    @Test
    void aRehomeThenASupersedeOfTheSource_givesTheSlugTargetTheDocumentsOwner() {
        String x = "code__xs-aa11bb22__voyage-code-3__v1";
        String y = "code__ys-cc33dd44__voyage-code-3__v1";
        register(x, "xs-aa11bb22");
        register(y, "ys-cc33dd44");
        putDoc("1.44.1", x);
        assertThat(ownerOf(x)).as("the source takes its documents' owner (free tuple)").isEqualTo("1-44");

        repo.rehomeCollection(TENANT, x, y);
        assertThat(ownerOf(y)).as("the source is still live and holds 1-44, so the target keeps its slug")
            .isEqualTo("ys-cc33dd44");

        repo.supersedeCollection(TENANT, x, y, null);
        assertThat(ownerOf(y)).as("the source is retired: the target takes the documents' owner").isEqualTo("1-44");
    }

    @Test
    void aDeleteOfTheHolder_givesTheSlugCollectionItHeldBackTheDocumentsOwner() {
        String x = "code__xd-aa11bb22__voyage-code-3__v1";
        String y = "code__yd-cc33dd44__voyage-code-3__v1";
        register(x, "xd-aa11bb22");
        register(y, "yd-cc33dd44");
        putDoc("1.45.1", x);
        assertThat(ownerOf(x)).isEqualTo("1-45");
        putDoc("1.45.2", y);
        assertThat(ownerOf(y)).as("the other collection holds 1-45, so the slug stays").isEqualTo("yd-cc33dd44");

        repo.deleteCollection(TENANT, x);
        assertThat(ownerOf(y)).as("the holder is gone: the slug row takes the documents' owner").isEqualTo("1-45");
    }

    private void register(String name, String owner) {
        repo.upsertCollection(TENANT, Map.of(
            "name", name, "content_type", "code", "owner_id", owner,
            "embedding_model", MODEL, "model_version", "v1", "lifecycle_state", "live"));
    }

    private void putDoc(String tumbler, String collection) {
        Map<String, Object> doc = new HashMap<>();
        doc.put("tumbler", tumbler);
        doc.put("title", "doc " + tumbler);
        doc.put("content_type", "code");
        doc.put("physical_collection", collection);
        doc.put("source_uri", "file:///cat049d/" + tumbler);
        repo.upsertDocument(TENANT, doc);
    }

    private String ownerOf(String name) {
        return (String) repo.getCollection(TENANT, name).get("owner_id");
    }
}
