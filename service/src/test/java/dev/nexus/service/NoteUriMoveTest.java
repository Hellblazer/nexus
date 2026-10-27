// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-231 (nexus-ql5m5): the collection rename and the re-home rewrite a moved note's
 * source_uri in both spellings, in every table that holds one, and refuse a move whose
 * rewritten URI a live document already holds. RDR Test Plan rows 550, 551 and 552.
 *
 * <p>Each satellite row's URI is checked against its own derivation (catalog: the title;
 * aspect: the source_path, seeded different from the title), never against the catalog
 * row's URI (critique Significant 8).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NoteUriMoveTest {

    private static final String SVC_ROLE = "svc_note_uri_move_test";
    private static final String SVC_PASS = "svc_note_uri_move_test_pass";
    private static final String TENANT = "note-uri-move-tenant";
    private static final String TS = "2026-10-27T00:00:00Z";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    AspectRepository aspectRepo;
    PgVectorRepository vecRepo;

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
        catalogRepo = new CatalogRepository(tenantScope);
        aspectRepo = new AspectRepository(tenantScope);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── re-home (Test Plan rows 551, 552) ─────────────────────────────────────

    @Test
    void rehome_rewritesNoteUrisInEveryTable_andTheTargetLookupFindsTheNote() throws Exception {
        String src = coll("rh-src");
        String dst = coll("rh-dst");
        register(src, dst);
        seedNote("rh-note", src, "nx-note://" + src + "/decision");
        seedAspect("rh-note", src, "notes/decision.md", "nx-note://" + src + "/notes/decision.md");
        seedHighlight("rh-note", src, "nx-note://" + src + "/decision");
        seedNote("rh-legacy", src, "chroma://" + src + "/legacy-note");
        seedNote("rh-gone", src, "nx-note://" + src + "/gone");
        tombstone("rh-gone");
        seedNote("rh-file", src, "file:///papers/a.pdf");

        catalogRepo.rehomeCollection(TENANT, src, dst);

        assertThat(docUri("rh-note")).isEqualTo("nx-note://" + dst + "/decision");
        assertThat(aspectUri("rh-note")).isEqualTo("nx-note://" + dst + "/notes/decision.md");
        assertThat(highlightUri("rh-note")).isEqualTo("nx-note://" + dst + "/decision");
        assertThat(docUri("rh-legacy")).as("a legacy note keeps its spelling; the backfill owns the scheme")
            .isEqualTo("chroma://" + dst + "/legacy-note");
        assertThat(docUri("rh-gone")).as("a tombstone moves with its URI rewritten")
            .isEqualTo("nx-note://" + dst + "/gone");
        assertThat(docUri("rh-file")).isEqualTo("file:///papers/a.pdf");
        assertThat(columnsHolding("nx-note://" + src + "/")).isEmpty();
        assertThat(columnsHolding("chroma://" + src + "/")).isEmpty();

        // The lookup a re-put under the target makes finds the moved note, in either spelling.
        for (String asked : List.of("nx-note://" + dst + "/decision", "chroma://" + dst + "/decision")) {
            var found = catalogRepo.documentsBySourceUri(TENANT, asked, 0, 0);
            assertThat(found).as(asked).hasSize(1);
            assertThat(found.get(0).get("tumbler")).isEqualTo("rh-note");
        }
    }

    @Test
    void rehome_ontoATakenUri_refusesWholeNamingTheHolder() throws Exception {
        for (String scheme : List.of("nx-note://", "chroma://")) {
            String slug = scheme.startsWith("nx") ? "rht" : "rhl";
            String src = coll(slug + "-src");
            String dst = coll(slug + "-dst");
            register(src, dst);
            seedNote(slug + "-mover", src, scheme + src + "/t");
            seedNote(slug + "-other", src, "nx-note://" + src + "/other");
            seedNote(slug + "-holder", dst, scheme + dst + "/t");
            String chash = Chash.ofText(slug + "-mover-text").toHex();
            vecRepo.upsertChunks(TENANT, src, List.of(chash), List.of(slug + " mover text"), List.of(Map.of()));
            catalogRepo.writeManifest(TENANT, slug + "-mover", src, List.of(Map.of("position", 0, "chash", chash)));

            assertThatThrownBy(() -> catalogRepo.rehomeCollection(TENANT, src, dst))
                .as(scheme)
                .isInstanceOfSatisfying(CatalogRepository.SourceUriCollision.class, e -> {
                    assertThat(e.holder()).isEqualTo(slug + "-holder");
                    assertThat(e.getMessage()).contains("re-homing " + src).contains(slug + "-holder")
                        .contains(scheme + dst + "/t");
                });

            assertThat(physicalCollection(slug + "-mover")).isEqualTo(src);
            assertThat(physicalCollection(slug + "-other")).isEqualTo(src);
            assertThat(docUri(slug + "-mover")).isEqualTo(scheme + src + "/t");
            assertThat(docUri(slug + "-other")).isEqualTo("nx-note://" + src + "/other");
            assertThat(manifestCollections(slug + "-mover")).containsOnly(src);
            assertThat(chunkCount(src)).isEqualTo(1);
        }
    }

    @Test
    void rehome_aTombstonedHolderDoesNotBlock() throws Exception {
        String src = coll("rhd-src");
        String dst = coll("rhd-dst");
        register(src, dst);
        seedNote("rhd-mover", src, "nx-note://" + src + "/t");
        seedNote("rhd-dead", dst, "nx-note://" + dst + "/t");
        tombstone("rhd-dead");

        catalogRepo.rehomeCollection(TENANT, src, dst);

        assertThat(docUri("rhd-mover")).isEqualTo("nx-note://" + dst + "/t");
    }

    // ── rename (Test Plan row 550) ────────────────────────────────────────────

    @Test
    void rename_rewritesBothSpellings_leavesResidueAndLookalikesAlone() throws Exception {
        String oldName = coll("rn_old");
        String newName = coll("rn-new");
        // '_' is a LIKE wildcard: this lookalike collection name differs from oldName at the '_'.
        String lookalike = oldName.replace("rn_old", "rnXold");
        register(oldName);
        seedNote("rn-note", oldName, "nx-note://" + oldName + "/n");
        seedNote("rn-legacy", oldName, "chroma://" + oldName + "/l");
        seedNote("rn-like", oldName, "nx-note://" + lookalike + "/x");
        seedNote("rn-rdr", oldName, "chroma://rdr__1-1__voyage-context-3__v1/docs/a.md");
        seedAspect("rn-note", oldName, "src/n.md", "nx-note://" + oldName + "/src/n.md");
        seedHighlight("rn-note", oldName, "nx-note://" + oldName + "/n");

        catalogRepo.renameCollection(TENANT, oldName, newName);

        assertThat(docUri("rn-note")).isEqualTo("nx-note://" + newName + "/n");
        assertThat(docUri("rn-legacy")).isEqualTo("chroma://" + newName + "/l");
        assertThat(docUri("rn-like")).isEqualTo("nx-note://" + lookalike + "/x");
        assertThat(docUri("rn-rdr")).isEqualTo("chroma://rdr__1-1__voyage-context-3__v1/docs/a.md");
        assertThat(aspectUri("rn-note")).isEqualTo("nx-note://" + newName + "/src/n.md");
        assertThat(highlightUri("rn-note")).isEqualTo("nx-note://" + newName + "/n");
    }

    @Test
    void rename_preCheckFiresForTheNewSpelling() throws Exception {
        String oldName = coll("rnc-old");
        String newName = coll("rnc-new");
        String elsewhere = coll("rnc-else");
        register(oldName, elsewhere);
        seedNote("rnc-mover", oldName, "nx-note://" + oldName + "/t");
        seedNote("rnc-holder", elsewhere, "nx-note://" + newName + "/t");

        assertThatThrownBy(() -> catalogRepo.renameCollection(TENANT, oldName, newName))
            .isInstanceOfSatisfying(CatalogRepository.SourceUriCollision.class,
                e -> assertThat(e.holder()).isEqualTo("rnc-holder"));
        assertThat(docUri("rnc-mover")).isEqualTo("nx-note://" + oldName + "/t");
        assertThat(physicalCollection("rnc-mover")).isEqualTo(oldName);
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private static String coll(String slug) {
        return "knowledge__" + slug + "__minilm-l6-v2-384__v1";
    }

    private void register(String... names) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String n : names) PgContainerHelper.insertCollection(dsl, TENANT, n);
        }
    }

    private void seedNote(String tumbler, String collection, String uri) throws Exception {
        if (uri.startsWith("chroma://")) {
            // A legacy row as the backfill finds it: written below the repository, which
            // would store the nx-note:// spelling.
            catalogRepo.upsertDocument(TENANT, Map.of(
                "tumbler", tumbler, "title", tumbler, "content_type", "knowledge",
                "corpus", "knowledge", "physical_collection", collection));
            setUri(tumbler, uri);
            return;
        }
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", collection, "source_uri", uri));
    }

    private void seedAspect(String docId, String collection, String sourcePath, String uri) {
        Map<String, Object> body = new HashMap<>();
        body.put("doc_id", docId);
        body.put("collection", collection);
        body.put("source_path", sourcePath);
        body.put("source_uri", uri);
        body.put("extracted_at", TS);
        body.put("model_version", "test");
        body.put("extractor_name", "test");
        body.put("confidence", 0.9);
        aspectRepo.upsertAspect(TENANT, body);
    }

    private void seedHighlight(String docId, String collection, String uri) {
        aspectRepo.upsertHighlight(TENANT, Map.of(
            "doc_id", docId, "collection", collection, "source_uri", uri,
            "highlights_md", "- a highlight", "ingested_at", TS));
    }

    private void tombstone(String tumbler) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, java.time.OffsetDateTime.now())
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
        }
    }

    private void setUri(String tumbler, String uri) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.SOURCE_URI, uri)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
                .execute();
        }
    }

    private String docUri(String tumbler) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }

    private String aspectUri(String docId) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.select(DOCUMENT_ASPECTS.SOURCE_URI)
            .from(DOCUMENT_ASPECTS).where(DOCUMENT_ASPECTS.DOC_ID.eq(docId)).fetchOne(0, String.class));
    }

    private String highlightUri(String docId) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.select(DOCUMENT_HIGHLIGHTS.SOURCE_URI)
            .from(DOCUMENT_HIGHLIGHTS).where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId)).fetchOne(0, String.class));
    }

    private String physicalCollection(String tumbler) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }

    private List<String> manifestCollections(String tumbler) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.select(CATALOG_DOCUMENT_CHUNKS.COLLECTION)
            .from(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(tumbler)).fetch(0, String.class));
    }

    private int chunkCount(String collection) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection)));
    }

    /** Every (table.column) in nexus whose text value contains {@code needle}, over all source_uri columns. */
    private List<String> columnsHolding(String needle) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            var col = DSL.field(DSL.name("columns", "table_name"), String.class);
            var tables = dsl.select(col).from(DSL.table(DSL.name("information_schema", "columns")).as("columns"))
                .join(DSL.table(DSL.name("information_schema", "tables")).as("tables"))
                .on(DSL.field(DSL.name("tables", "table_name"), String.class).eq(col)
                    .and(DSL.field(DSL.name("tables", "table_schema"), String.class)
                        .eq(DSL.field(DSL.name("columns", "table_schema"), String.class))))
                .where(DSL.field(DSL.name("columns", "table_schema"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("columns", "column_name"), String.class).eq("source_uri"))
                .and(DSL.field(DSL.name("tables", "table_type"), String.class).eq("BASE TABLE"))
                .fetch(0, String.class);
            assertThat(tables).as("the sweep must see the URI columns it checks").contains("catalog_documents");
            List<String> hits = new ArrayList<>();
            for (String t : tables) {
                boolean holds = dsl.fetchExists(dsl.selectOne().from(DSL.table(DSL.name("nexus", t)))
                    .where(DSL.function("strpos", Integer.class,
                        DSL.field(DSL.name("source_uri"), String.class), DSL.val(needle)).gt(0)));
                if (holds) hits.add(t + ".source_uri");
            }
            return hits;
        }
    }
}
