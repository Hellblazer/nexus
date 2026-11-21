// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.NoteUri;
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
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P2.10 (nexus-p2m1x.15), the rule test. A file-routed collection's documents carry {@code chroma://} residue
 * and an {@code nx-note://<file-routed>} value. Moving them onto a prefix-free label by a rename, a re-home or a
 * collection update leaves both values, in all three tables, on their old segment. Both are still classed file-routed
 * by {@link NoteUri#isNote}, and the stale-note repair counts nothing. (The re-embed move and the reconcile commit
 * have the same case in {@code DocumentMoveTest} and {@code CatalogReconcileCommitNoteUriRuleTest}, which own their
 * fixtures.)
 *
 * <p>Each test plants its own tenant: the repair's census counts the whole tenant, so a count is exact only when
 * nothing else is in it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NoteUriResidueRuleTest {

    private static final String SVC_ROLE = "svc_note_uri_residue_rule_test";
    private static final String SVC_PASS = "svc_note_uri_residue_rule_test_pass";
    private static final String TS = "2026-11-20T00:00:00Z";
    private static final AtomicInteger SEQ = new AtomicInteger();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository repo;
    AspectRepository aspectRepo;

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
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        aspectRepo = new AspectRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** One planting: a file-routed collection, the label the move targets, and the planted values. */
    private record Plant(String tenant, String src, String label, String residue, String fileNote) {}

    private Plant plant(boolean registerLabel) throws Exception {
        int n = SEQ.incrementAndGet();
        String tenant = "residue-rule-" + n;
        String src = "docs__rr" + n + "__minilm-l6-v2-384__v1";
        String label = "plain-label-" + n;
        Plant p = new Plant(tenant, src, label, "chroma://" + src + "/a.md", "nx-note://" + src + "/b.md");
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, tenant, src);
            if (registerLabel) PgContainerHelper.insertCollection(dsl, tenant, label);
        }
        seed(p, "res", p.residue());
        seed(p, "nn", p.fileNote());
        return p;
    }

    /** A document under the file-routed collection with the value planted below the repository, plus both satellites. */
    private void seed(Plant p, String tumbler, String uri) throws Exception {
        repo.upsertDocument(p.tenant(), Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "docs",
            "corpus", "docs", "physical_collection", p.src()));
        Map<String, Object> aspect = new HashMap<>();
        aspect.put("doc_id", tumbler);
        aspect.put("collection", p.src());
        aspect.put("source_path", "docs/" + tumbler + ".md");
        aspect.put("source_uri", "file:///seed/" + tumbler);
        aspect.put("extracted_at", TS);
        aspect.put("model_version", "test");
        aspect.put("extractor_name", "test");
        aspect.put("confidence", 0.9);
        aspectRepo.upsertAspect(p.tenant(), aspect);
        aspectRepo.upsertHighlight(p.tenant(), Map.of(
            "doc_id", tumbler, "collection", p.src(), "source_uri", "file:///seed/" + tumbler,
            "highlights_md", "- a highlight", "ingested_at", TS));
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, uri)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(p.tenant()).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute();
            dsl.update(DOCUMENT_ASPECTS).set(DOCUMENT_ASPECTS.SOURCE_URI, uri)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(p.tenant()).and(DOCUMENT_ASPECTS.DOC_ID.eq(tumbler))).execute();
            dsl.update(DOCUMENT_HIGHLIGHTS).set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, uri)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(p.tenant()).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(tumbler))).execute();
        }
    }

    private void assertUntouched(Plant p, String how) {
        for (String t : List.of("res", "nn")) {
            String expected = t.equals("res") ? p.residue() : p.fileNote();
            assertThat(docUri(p, t)).as(how + ": catalog_documents " + t).isEqualTo(expected);
            assertThat(aspectUri(p, t)).as(how + ": document_aspects " + t).isEqualTo(expected);
            assertThat(highlightUri(p, t)).as(how + ": document_highlights " + t).isEqualTo(expected);
            assertThat(NoteUri.isNote(expected)).as(how + ": still file-routed, not a note").isFalse();
        }
        var census = repo.repairStaleNoteUris(p.tenant(), false, 1000, 10);
        assertThat(census.get("stale_live")).as(how + ": the stale-note repair skips both").isEqualTo(0);
        assertThat(census.get("rewrite")).isEqualTo(0);
    }

    @Test
    void rename_leavesResidueAndAFileRoutedNoteValueOnTheirOldSegment() throws Exception {
        Plant p = plant(false);   // a rename's target must not exist yet

        repo.renameCollection(p.tenant(), p.src(), p.label());

        assertThat(physical(p, "res")).as("the documents did move").isEqualTo(p.label());
        assertThat(physical(p, "nn")).isEqualTo(p.label());
        assertUntouched(p, "rename");
    }

    @Test
    void rehome_leavesResidueAndAFileRoutedNoteValueOnTheirOldSegment() throws Exception {
        Plant p = plant(true);

        repo.rehomeCollection(p.tenant(), p.src(), p.label());

        assertThat(physical(p, "res")).isEqualTo(p.label());
        assertThat(physical(p, "nn")).isEqualTo(p.label());
        assertUntouched(p, "re-home");
    }

    @Test
    void collectionUpdate_leavesResidueAndAFileRoutedNoteValueOnTheirOldSegment() throws Exception {
        Plant p = plant(true);

        repo.updateDocument(p.tenant(), "res", Map.of("physical_collection", p.label()));
        repo.updateDocument(p.tenant(), "nn", Map.of("physical_collection", p.label()));

        assertThat(physical(p, "res")).isEqualTo(p.label());
        assertThat(physical(p, "nn")).isEqualTo(p.label());
        assertThat(docUri(p, "res")).isEqualTo(p.residue());
        assertThat(docUri(p, "nn")).isEqualTo(p.fileNote());
        var census = repo.repairStaleNoteUris(p.tenant(), false, 1000, 10);
        assertThat(census.get("stale_live")).as("the stale-note repair skips both").isEqualTo(0);
    }

    /** The pre-check goes through the same rule: a value a move would not make cannot be held. */
    @Test
    void aValueTheRuleWouldNotMakeIsNoCollision() throws Exception {
        Plant p = plant(true);
        // a live holder of exactly the value the old rule would have made out of the residue
        repo.upsertDocument(p.tenant(), Map.of(
            "tumbler", "holder", "title", "holder", "content_type", "docs", "corpus", "docs",
            "physical_collection", p.label()));
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, "chroma://" + p.label() + "/a.md")
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(p.tenant()).and(CATALOG_DOCUMENTS.TUMBLER.eq("holder"))).execute();
        }

        repo.rehomeCollection(p.tenant(), p.src(), p.label());

        assertThat(docUri(p, "res")).isEqualTo(p.residue());
    }

    private String physical(Plant p, String tumbler) {
        return tenantScope.withTenant(p.tenant(), ctx -> ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }

    private String docUri(Plant p, String tumbler) {
        return tenantScope.withTenant(p.tenant(), ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }

    private String aspectUri(Plant p, String docId) {
        return tenantScope.withTenant(p.tenant(), ctx -> ctx.select(DOCUMENT_ASPECTS.SOURCE_URI)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.DOC_ID.eq(docId)).fetchOne(0, String.class));
    }

    private String highlightUri(Plant p, String docId) {
        return tenantScope.withTenant(p.tenant(), ctx -> ctx.select(DOCUMENT_HIGHLIGHTS.SOURCE_URI)
            .from(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId)).fetchOne(0, String.class));
    }
}
