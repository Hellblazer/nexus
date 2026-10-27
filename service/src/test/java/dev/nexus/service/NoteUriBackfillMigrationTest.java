// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.InputStream;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-231 (nexus-bivz3.5): {@code note-uri-001-1} against a populated database, run as the migration's own
 * non-BYPASSRLS owner under FORCE row-level security (so a missing NO FORCE toggle shows as a rewrite that
 * changed nothing).
 *
 * <p>One walk, three phases over the same cluster. (1) A live note with a live {@code nx-note://} twin in the
 * direct form: the migration must fail with the operator's recovery text, change no row in any of the three
 * tables, and leave FORCE set on all three (RDR Test Plan row 559). (2) The same for the export form
 * ({@code nxexp://<T>/nx-note://...} beside {@code nxexp://<T>/chroma://...}). (3) With the twins gone the
 * migration passes and rewrites live and tombstoned notes in all three tables by scheme only, leaving
 * file-routed residue, the residue under an export identity, rows already in the new spelling, other schemes,
 * and the collection of a stale-collection note alone. A tombstoned twin and a twin in another tenant never
 * trip the guard. Each phase first asserts the pre-state, so a run that seeded nothing cannot pass.
 */
class NoteUriBackfillMigrationTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TARGET = "note-uri-001-1";
    private static final String T1 = "nb5-tenant-1";
    private static final String T2 = "nb5-tenant-2";
    private static final String NOTES = "knowledge__nb5";
    private static final String NOTES_Y = "knowledge__nb5y";
    private static final String DOCS = "docs__nb5";
    private static final String RDR = "rdr__nb5";
    private static final List<String> TABLES = List.of("catalog_documents", "document_aspects", "document_highlights");

    @Test
    void guardAbortsOnALiveTwinChangingNothing_thenTheBackfillRewritesNotesOnly() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            final String role = "nexus_admin_noteuri_test";
            final String pass = "nexus_admin_noteuri_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-noteuri-test");
            try (HikariDataSource adminDs = new HikariDataSource(cfg)) {
                migrateUpTo(adminDs, TARGET);

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seed(DSL.using(su, SQLDialect.POSTGRES));
                }
                Map<String, String> before;
                try (Connection su = pg.createConnection("")) {
                    before = snapshot(DSL.using(su, SQLDialect.POSTGRES));
                }
                assertThat(before).as("non-vacuity: the seed holds chroma:// notes").containsValue("chroma://knowledge__nb5/n1");
                assertThat(forceFlags(pg)).as("FORCE is set on all three tables before the migration")
                    .containsOnly(Map.entry("catalog_documents", true), Map.entry("document_aspects", true),
                        Map.entry("document_highlights", true));

                // ── phase 1: a live direct-form twin of 1.1.1 ──
                try (Connection su = pg.createConnection("")) {
                    insertDoc(DSL.using(su, SQLDialect.POSTGRES), T1, "1.1.20", NOTES, "nx-note://knowledge__nb5/n1", false);
                }
                Map<String, String> beforeDirect;
                try (Connection su = pg.createConnection("")) {
                    beforeDirect = snapshot(DSL.using(su, SQLDialect.POSTGRES));
                }
                assertAbortsChangingNothing(adminDs, pg, beforeDirect, "direct form");

                // ── phase 2: a live export-form twin of 1.1.3 (the direct twin is gone) ──
                try (Connection su = pg.createConnection("")) {
                    DSLContext c = DSL.using(su, SQLDialect.POSTGRES);
                    c.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(T1))
                        .and(CATALOG_DOCUMENTS.TUMBLER.eq("1.1.20")).execute();
                    insertDoc(c, T1, "1.1.21", NOTES, "nxexp://knowledge__imp/nx-note://knowledge__nb5/n3", false);
                }
                Map<String, String> beforeExport;
                try (Connection su = pg.createConnection("")) {
                    beforeExport = snapshot(DSL.using(su, SQLDialect.POSTGRES));
                }
                assertThat(beforeExport).as("the direct twin is gone").doesNotContainKey(T1 + "/1.1.20");
                assertAbortsChangingNothing(adminDs, pg, beforeExport, "export form");

                // ── phase 3: no live twin left; the backfill passes ──
                try (Connection su = pg.createConnection("")) {
                    DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENTS)
                        .where(CATALOG_DOCUMENTS.TENANT_ID.eq(T1)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.1.21")).execute();
                }
                applyRemainingChangelog(adminDs);

                try (Connection su = pg.createConnection("")) {
                    DSLContext c = DSL.using(su, SQLDialect.POSTGRES);
                    Map<String, String> after = snapshot(c);
                    assertThat(after.get(T1 + "/1.1.1")).as("live direct-form note").isEqualTo("nx-note://knowledge__nb5/n1");
                    assertThat(after.get(T1 + "/1.1.2")).as("tombstoned note is rewritten too")
                        .isEqualTo("nx-note://knowledge__nb5/n2");
                    assertThat(after.get(T1 + "/1.1.3")).as("export-form note")
                        .isEqualTo("nxexp://knowledge__imp/nx-note://knowledge__nb5/n3");
                    assertThat(after.get(T1 + "/1.1.4")).as("file-routed residue keeps its spelling")
                        .isEqualTo("chroma://docs__nb5/f.md");
                    assertThat(after.get(T1 + "/1.1.5")).as("residue under an export identity keeps its spelling")
                        .isEqualTo("nxexp://knowledge__imp/chroma://rdr__nb5/f.md");
                    assertThat(after.get(T1 + "/1.1.6")).as("a row already in the new spelling").isEqualTo("nx-note://knowledge__nb5/n6");
                    assertThat(after.get(T1 + "/1.1.7")).as("another scheme").isEqualTo("file:///x.md");
                    assertThat(after.get(T1 + "/1.1.8")).as("a stale-collection note is rewritten by scheme only")
                        .isEqualTo("nx-note://knowledge__nb5x/n8");
                    assertThat(c.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION).from(CATALOG_DOCUMENTS)
                            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(T1)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.1.8"))
                            .fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION))
                        .as("the stale note's collection is not repaired").isEqualTo(NOTES_Y);
                    assertThat(after.get(T1 + "/1.1.9")).as("live note whose twin is tombstoned is rewritten")
                        .isEqualTo("nx-note://knowledge__nb5/tw");
                    assertThat(after.get(T1 + "/1.1.10")).as("the tombstoned twin is untouched")
                        .isEqualTo("nx-note://knowledge__nb5/tw");
                    assertThat(after.get(T1 + "/1.1.11")).as("a tombstoned note beside a live twin is rewritten, not a halt")
                        .isEqualTo("nx-note://knowledge__nb5/dead");
                    assertThat(after.get(T1 + "/1.1.12")).as("the live twin of a tombstoned note is untouched")
                        .isEqualTo("nx-note://knowledge__nb5/dead");
                    assertThat(after.get(T2 + "/1.1.1")).as("another tenant's row is untouched")
                        .isEqualTo("nx-note://knowledge__nb5/n1");

                    var asp = aspectUris(c);
                    assertThat(asp.get("p1")).isEqualTo("nx-note://knowledge__nb5/p1");
                    assertThat(asp.get("p2")).as("aspect residue").isEqualTo("chroma://docs__nb5/p2");
                    assertThat(asp.get("p3")).isEqualTo("nxexp://knowledge__imp/nx-note://knowledge__nb5/p3");
                    assertThat(asp.get("p4")).as("aspect export residue")
                        .isEqualTo("nxexp://knowledge__imp/chroma://rdr__nb5/p4");
                    assertThat(asp.get("p5")).as("aspect note, stale collection")
                        .isEqualTo("nx-note://knowledge__nb5x/p5");

                    var hl = highlightUris(c);
                    assertThat(hl.get("1.1.1")).isEqualTo("nx-note://knowledge__nb5/hl-1.1.1");
                    assertThat(hl.get("1.1.3")).isEqualTo("nxexp://knowledge__imp/nx-note://knowledge__nb5/hl-1.1.3");
                    assertThat(hl.get("1.1.4")).as("highlight residue").isEqualTo("chroma://docs__nb5/hl-1.1.4");
                    assertThat(hl.get("1.1.8")).as("highlight note, stale collection")
                        .isEqualTo("nx-note://knowledge__nb5x/hl-1.1.8");
                }
                assertThat(forceFlags(pg)).as("FORCE is set on all three tables after the migration")
                    .containsOnly(Map.entry("catalog_documents", true), Map.entry("document_aspects", true),
                        Map.entry("document_highlights", true));
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The changeset's own SQL, run over every row of the shared corpus note-uri-corpus.json (one tenant per
     * row, so the corpus's chroma:// and nx-note:// twins cannot trip the guard): each value ends up as the
     * corpus's {@code canonical}. A second tenant per nx-note:// row holds its chroma:// spelling, which the
     * backfill rewrites exactly when the corpus says the nx-note:// row is a note, so the collection test
     * agrees with {@code is_note} on both spellings.
     */
    @Test
    void corpusRowsEndUpInTheirCanonicalForm() throws Exception {
        JsonNode rows;
        try (InputStream in = NoteUriBackfillMigrationTest.class.getResourceAsStream("/note-uri-corpus.json")) {
            assertThat(in).as("resource note-uri-corpus.json").isNotNull();
            rows = new ObjectMapper().readTree(in).get("rows");
        }
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            final String role = "nexus_admin_noteuri_corpus";
            final String pass = "nexus_admin_noteuri_corpus_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-noteuri-corpus");
            Map<String, String> expected = new LinkedHashMap<>();
            Map<String, String> seeded = new LinkedHashMap<>();
            try (HikariDataSource adminDs = new HikariDataSource(cfg)) {
                migrateUpTo(adminDs, TARGET);
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext c = DSL.using(su, SQLDialect.POSTGRES);
                    int i = 0;
                    for (JsonNode row : rows) {
                        String uri = row.get("uri").asText();
                        String tenant = "nbc-" + i++;
                        insertDoc(c, tenant, "1.3.1", "", uri, false);
                        seeded.put(tenant, uri);
                        expected.put(tenant, row.get("canonical").asText());
                        if (uri.startsWith("nx-note://")) {
                            String legacy = "chroma://" + uri.substring("nx-note://".length());
                            String legacyTenant = "nbc-" + i++;
                            insertDoc(c, legacyTenant, "1.3.1", "", legacy, false);
                            seeded.put(legacyTenant, legacy);
                            // the corpus says whether the nx-note:// value is a note; its chroma:// spelling
                            // is then a note too, and is rewritten to the nx-note:// value
                            expected.put(legacyTenant, row.get("is_note").asBoolean() ? uri : legacy);
                        }
                    }
                }
                applyRemainingChangelog(adminDs);
            }
            int changed = 0;
            int examined = 0;
            try (Connection su = pg.createConnection("")) {
                DSLContext c = DSL.using(su, SQLDialect.POSTGRES);
                for (var e : expected.entrySet()) {
                    String tenant = e.getKey();
                    String actual = c.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                        .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq("1.3.1"))
                        .fetchOne(CATALOG_DOCUMENTS.SOURCE_URI);
                    assertThat(actual).as("the backfill of '%s'", seeded.get(tenant)).isEqualTo(e.getValue());
                    examined++;
                    if (!actual.equals(seeded.get(tenant))) {
                        changed++;
                    }
                }
            }
            assertThat(examined).as("rows examined").isEqualTo(seeded.size()).isGreaterThanOrEqualTo(rows.size());
            assertThat(changed).as("rows the backfill rewrote (notes and export notes)").isGreaterThanOrEqualTo(6);
        } finally {
            pg.stop();
        }
    }

    private static void assertAbortsChangingNothing(HikariDataSource adminDs, PostgreSQLContainer<?> pg,
                                                    Map<String, String> expected, String form) throws Exception {
        assertThatThrownBy(() -> applyRemainingChangelog(adminDs))
            .as("a live twin (%s) must stop the migration", form)
            .satisfies(t -> {
                StringBuilder chain = new StringBuilder();
                for (Throwable x = t; x != null; x = x.getCause()) {
                    chain.append(x.getMessage()).append('\n');
                }
                assertThat(chain.toString())
                    .contains("nothing was changed")
                    .contains("1 live chroma:// note row(s) have a live nx-note:// twin")
                    .contains("Start the previous engine binary")
                    .contains("POST /v1/catalog/merge")
                    .contains("restart on the new engine")
                    .contains("https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md#"
                        + "if-the-rdr-231-note-uri-backfill-halts-startup");
            });
        try (Connection su = pg.createConnection("")) {
            DSLContext c = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(snapshot(c)).as("no catalog_documents row changed (%s)", form).isEqualTo(expected);
            assertThat(aspectUris(c)).as("no document_aspects row changed (%s)", form).containsEntry("p1", "chroma://knowledge__nb5/p1");
            assertThat(highlightUris(c)).as("no document_highlights row changed (%s)", form)
                .containsEntry("1.1.1", "chroma://knowledge__nb5/hl-1.1.1");
        }
        assertThat(forceFlags(pg)).as("FORCE is still set on all three tables after the abort (%s)", form)
            .containsOnly(Map.entry("catalog_documents", true), Map.entry("document_aspects", true),
                Map.entry("document_highlights", true));
    }

    private static void seed(DSLContext c) {
        for (String coll : List.of(NOTES, NOTES_Y, DOCS, RDR)) {
            PgContainerHelper.insertCollection(c, T1, coll);
        }
        PgContainerHelper.insertCollection(c, T2, NOTES);

        insertDoc(c, T1, "1.1.1", NOTES, "chroma://knowledge__nb5/n1", false);
        insertDoc(c, T1, "1.1.2", NOTES, "chroma://knowledge__nb5/n2", true);
        insertDoc(c, T1, "1.1.3", NOTES, "nxexp://knowledge__imp/chroma://knowledge__nb5/n3", false);
        insertDoc(c, T1, "1.1.4", DOCS, "chroma://docs__nb5/f.md", false);
        insertDoc(c, T1, "1.1.5", RDR, "nxexp://knowledge__imp/chroma://rdr__nb5/f.md", false);
        insertDoc(c, T1, "1.1.6", NOTES, "nx-note://knowledge__nb5/n6", false);
        insertDoc(c, T1, "1.1.7", NOTES, "file:///x.md", false);
        insertDoc(c, T1, "1.1.8", NOTES_Y, "chroma://knowledge__nb5x/n8", false);
        insertDoc(c, T1, "1.1.9", NOTES, "chroma://knowledge__nb5/tw", false);
        insertDoc(c, T1, "1.1.10", NOTES, "nx-note://knowledge__nb5/tw", true);
        // a TOMBSTONED chroma:// note beside a LIVE nx-note:// twin: catalog-016 constrains live rows only, so
        // the guard (o.deleted_at IS NULL) must neither count it nor halt on it
        insertDoc(c, T1, "1.1.11", NOTES, "chroma://knowledge__nb5/dead", true);
        insertDoc(c, T1, "1.1.12", NOTES, "nx-note://knowledge__nb5/dead", false);
        insertDoc(c, T2, "1.1.1", NOTES, "nx-note://knowledge__nb5/n1", false);

        insertAspect(c, "p1", NOTES, "chroma://knowledge__nb5/p1", "1.1.1");
        insertAspect(c, "p2", DOCS, "chroma://docs__nb5/p2", "1.1.4");
        insertAspect(c, "p3", NOTES, "nxexp://knowledge__imp/chroma://knowledge__nb5/p3", "1.1.3");
        insertAspect(c, "p4", RDR, "nxexp://knowledge__imp/chroma://rdr__nb5/p4", "1.1.5");
        insertAspect(c, "p5", NOTES_Y, "chroma://knowledge__nb5x/p5", "1.1.8");

        insertHighlight(c, "1.1.1", NOTES, "chroma://knowledge__nb5/hl-1.1.1");
        insertHighlight(c, "1.1.3", NOTES, "nxexp://knowledge__imp/chroma://knowledge__nb5/hl-1.1.3");
        insertHighlight(c, "1.1.4", DOCS, "chroma://docs__nb5/hl-1.1.4");
        insertHighlight(c, "1.1.8", NOTES_Y, "chroma://knowledge__nb5x/hl-1.1.8");
    }

    private static void insertDoc(DSLContext c, String tenant, String tumbler, String collection, String uri,
                                  boolean tombstoned) {
        c.insertInto(CATALOG_DOCUMENTS,
                CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI, CATALOG_DOCUMENTS.DELETED_AT)
            .values(tenant, tumbler, "doc " + tumbler, collection, uri, tombstoned ? OffsetDateTime.now() : null)
            .execute();
    }

    private static void insertAspect(DSLContext c, String path, String collection, String uri, String docId) {
        c.insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, T1)
            .set(DOCUMENT_ASPECTS.COLLECTION, collection)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, path)
            .set(DOCUMENT_ASPECTS.PROPOSED_METHOD, "note-uri backfill test")
            .set(DOCUMENT_ASPECTS.EXTRACTED_AT, DSL.currentOffsetDateTime())
            .set(DOCUMENT_ASPECTS.MODEL_VERSION, "test-model")
            .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test-extractor")
            .set(DOCUMENT_ASPECTS.SOURCE_URI, uri)
            .set(DOCUMENT_ASPECTS.DOC_ID, docId)
            .execute();
    }

    private static void insertHighlight(DSLContext c, String tumbler, String collection, String uri) {
        c.insertInto(DOCUMENT_HIGHLIGHTS)
            .set(DOCUMENT_HIGHLIGHTS.TENANT_ID, T1)
            .set(DOCUMENT_HIGHLIGHTS.DOC_ID, tumbler)
            .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, uri)
            .set(DOCUMENT_HIGHLIGHTS.COLLECTION, collection)
            .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT, DSL.currentOffsetDateTime())
            .execute();
    }

    /** {@code tenant/tumbler -> source_uri} for every catalog_documents row of the two tenants. */
    private static Map<String, String> snapshot(DSLContext c) {
        Map<String, String> out = new LinkedHashMap<>();
        c.select(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.SOURCE_URI)
            .from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.in(T1, T2))
            .orderBy(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
            .forEach(r -> out.put(r.value1() + "/" + r.value2(), r.value3()));
        return out;
    }

    private static Map<String, String> aspectUris(DSLContext c) {
        Map<String, String> out = new LinkedHashMap<>();
        c.select(DOCUMENT_ASPECTS.SOURCE_PATH, DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(T1))
            .forEach(r -> out.put(r.value1(), r.value2()));
        return out;
    }

    private static Map<String, String> highlightUris(DSLContext c) {
        Map<String, String> out = new LinkedHashMap<>();
        c.select(DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.SOURCE_URI).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(T1))
            .forEach(r -> out.put(r.value1(), r.value2()));
        return out;
    }

    /** {@code relforcerowsecurity} of each backfilled table, read as the superuser. */
    private static Map<String, Boolean> forceFlags(PostgreSQLContainer<?> pg) throws Exception {
        Map<String, Boolean> out = new LinkedHashMap<>();
        try (Connection su = pg.createConnection("")) {
            DSLContext c = DSL.using(su, SQLDialect.POSTGRES);
            for (String t : TABLES) {
                var rs = PgCatalogProbes.rowSecurity(c, "nexus", t);
                assertThat(rs).as("nexus." + t + " must exist").isNotNull();
                out.put(t, rs.forced());
            }
        }
        return out;
    }

    private static void applyRemainingChangelog(HikariDataSource adminDs) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        }
    }

    /** Applies the master changelog up to, not including, {@code targetChangesetId}. */
    private static void migrateUpTo(HikariDataSource adminDs, String targetChangesetId) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                List<String> ids = new ArrayList<>();
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    ids.add(unrun.get(i).getId());
                    if (targetChangesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as(targetChangesetId + " must be pending in the master changelog").isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
