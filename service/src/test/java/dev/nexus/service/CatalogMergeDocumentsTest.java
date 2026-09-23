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
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-z4rpi — {@link CatalogRepository#mergeDocuments} collapses a
 * duplicate catalog entry into its canonical one in ONE transaction.
 *
 * <p>Replaces the client-side three-call recipe ({@code update(dup,
 * source_uri='')} + {@code update(canonical, source_uri=<uri>)} +
 * {@code update(dup, alias_of=canonical)}) that {@code
 * ux_catalog_documents_live_source_uri} (catalog-016) forces apart and that
 * a failure between any two calls could tear — either leaving a document
 * with no identity at all, or two live rows both claiming the same document
 * with no alias between them.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogMergeDocumentsTest {

    private static final String TENANT_A = "merge-a";
    private static final String TENANT_B = "merge-b";
    private static final String SVC_ROLE = "svc_merge_test";
    private static final String SVC_PASS = "svc_merge_pass";

    PostgreSQLContainer<?> pg;
    CatalogRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

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
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String register(String tenant, String owner, String title, String sourceUri, String filePath) {
        var fields = new java.util.LinkedHashMap<String, Object>();
        fields.put("title", title);
        fields.put("file_path", filePath);
        if (sourceUri != null) fields.put("source_uri", sourceUri);
        return repo.registerDocument(tenant, owner, fields);
    }

    private Map<String, Object> readRow(String tenant, String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var r = DSL.using(su, SQLDialect.POSTGRES)
                .select(CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.ALIAS_OF)
                .from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .fetchOne();
            return Map.of("source_uri", r.value1() == null ? "" : r.value1(),
                          "alias_of", r.value2() == null ? "" : r.value2());
        }
    }

    // ── semantics ───────────────────────────────────────────────────────

    @Test
    void merge_movesSourceUriToCanonical_whenCanonicalLacksOne() throws Exception {
        String duplicate = register(TENANT_A, "30", "dup", "file:///d1/a.md", "a.md");
        String canonical = register(TENANT_A, "30", "canonical", null, "b.md");

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("source_uri_moved")).isEqualTo(true);

        var dupRow = readRow(TENANT_A, duplicate);
        var canonRow = readRow(TENANT_A, canonical);
        assertThat(dupRow.get("source_uri")).as("duplicate's identity slot is freed").isEqualTo("");
        assertThat(dupRow.get("alias_of")).as("duplicate points at canonical").isEqualTo(canonical);
        assertThat(canonRow.get("source_uri")).as("canonical takes the durable identity")
            .isEqualTo("file:///d1/a.md");
    }

    @Test
    void merge_doesNotMoveSourceUri_whenCanonicalAlreadyHasOne() throws Exception {
        String duplicate = register(TENANT_A, "31", "dup", "file:///d2/a.md", "a.md");
        String canonical = register(TENANT_A, "31", "canonical", "file:///d2/canonical.md", "canonical.md");

        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("source_uri_moved")).isEqualTo(false);

        var dupRow = readRow(TENANT_A, duplicate);
        var canonRow = readRow(TENANT_A, canonical);
        assertThat(dupRow.get("source_uri")).as("duplicate's identity slot is still freed").isEqualTo("");
        assertThat(dupRow.get("alias_of")).isEqualTo(canonical);
        assertThat(canonRow.get("source_uri")).as("canonical's own identity is untouched")
            .isEqualTo("file:///d2/canonical.md");
    }

    @Test
    void merge_isIdempotent_whenDuplicateAlreadyAliasedToSameCanonical() throws Exception {
        String duplicate = register(TENANT_A, "32", "dup", "file:///d3/a.md", "a.md");
        String canonical = register(TENANT_A, "32", "canonical", null, "b.md");

        repo.mergeDocuments(TENANT_A, duplicate, canonical);
        // Second call: the duplicate is now aliased to exactly this canonical
        // already — not "elsewhere" — so this must NOT refuse.
        var result = repo.mergeDocuments(TENANT_A, duplicate, canonical);
        assertThat(result.get("canonical")).isEqualTo(canonical);
        var dupRow = readRow(TENANT_A, duplicate);
        assertThat(dupRow.get("alias_of")).isEqualTo(canonical);
    }

    // ── refusals ────────────────────────────────────────────────────────

    @Test
    void merge_refusesSelfMerge() throws Exception {
        String doc = register(TENANT_A, "33", "solo", "file:///d4/a.md", "a.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, doc, doc))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("cannot merge a document with itself");
    }

    @Test
    void merge_refusesWhenDuplicateNotFound() {
        String canonical = register(TENANT_A, "34", "canonical", null, "b.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, "34.9999", canonical))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("duplicate not found");
    }

    @Test
    void merge_refusesWhenCanonicalNotFound() {
        String duplicate = register(TENANT_A, "35", "dup", "file:///d5/a.md", "a.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, "35.9999"))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("canonical not found");
    }

    @Test
    void merge_refusesCrossTenant() {
        // canonical registered under TENANT_B is invisible to a TENANT_A-scoped
        // merge call — RLS makes this structurally identical to "not found",
        // which IS the refusal (see MergeRefused's own javadoc).
        String duplicate = register(TENANT_A, "36", "dup", "file:///d6/a.md", "a.md");
        // A DIFFERENT owner literal than TENANT_A's, so the two registrations
        // cannot mint the same tumbler string and coincidentally read as a
        // self-merge instead of the cross-tenant case this test targets.
        String canonicalOtherTenant = register(TENANT_B, "136", "canonical-b", null, "b.md");
        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, canonicalOtherTenant))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("canonical not found");
    }

    @Test
    void merge_refusesAlreadyAliasedDuplicatePointingElsewhere() throws Exception {
        String duplicate = register(TENANT_A, "37", "dup", "file:///d7/a.md", "a.md");
        String otherCanonical = register(TENANT_A, "37", "other-canonical", null, "b.md");
        String newCanonical = register(TENANT_A, "37", "new-canonical", null, "c.md");
        repo.mergeDocuments(TENANT_A, duplicate, otherCanonical);  // dup -> otherCanonical

        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, newCanonical))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("already aliased to " + otherCanonical);
    }

    @Test
    void merge_refusesCycle() throws Exception {
        // canonical is already (transitively) an alias OF duplicate: pointing
        // duplicate -> canonical would close a loop.
        String duplicate = register(TENANT_A, "38", "dup", "file:///d8/a.md", "a.md");
        String canonical = register(TENANT_A, "38", "canonical", null, "b.md");
        repo.setAlias(TENANT_A, canonical, duplicate);  // canonical -> duplicate

        assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, canonical))
            .isInstanceOf(CatalogRepository.MergeRefused.class)
            .hasMessageContaining("alias cycle");
    }

    // ── atomicity ───────────────────────────────────────────────────────

    @Test
    void merge_midTransactionFailure_rollsBackEverything() throws Exception {
        // Force a failure on the LAST statement (the alias_of write) via a
        // throwaway CHECK constraint naming the canonical's own tumbler —
        // known only after registration, since tumblers are server-assigned.
        // The FIRST two statements (free duplicate's source_uri, move it onto
        // canonical) already executed inside the SAME uncommitted
        // transaction; this proves they roll back together rather than
        // leaving either row half-migrated.
        String duplicate = register(TENANT_A, "39", "dup", "file:///d9/a.md", "a.md");
        String canonical = register(TENANT_A, "39", "canonical", null, "b.md");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).execute(
                "ALTER TABLE nexus.catalog_documents ADD CONSTRAINT ck_merge_test_poison "
                + "CHECK (alias_of <> '" + canonical + "')");
        }
        try {
            assertThatThrownBy(() -> repo.mergeDocuments(TENANT_A, duplicate, canonical))
                .as("mid-transaction CHECK violation propagates")
                .isInstanceOf(Exception.class);

            var dupRow = readRow(TENANT_A, duplicate);
            var canonRow = readRow(TENANT_A, canonical);
            assertThat(dupRow.get("source_uri")).as("duplicate's source_uri NOT freed (rolled back)")
                .isEqualTo("file:///d9/a.md");
            assertThat(dupRow.get("alias_of")).as("duplicate NOT aliased (rolled back)").isEqualTo("");
            assertThat(canonRow.get("source_uri")).as("canonical did NOT receive the URI (rolled back)")
                .isEqualTo("");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).execute(
                    "ALTER TABLE nexus.catalog_documents DROP CONSTRAINT ck_merge_test_poison");
            }
        }
    }
}
