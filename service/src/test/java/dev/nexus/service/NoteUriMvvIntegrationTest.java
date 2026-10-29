// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeLogHistoryServiceFactory;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-231 Minimum Viable Validation (nexus-bivz3.6), on a real Postgres and the real HTTP service. Also RDR Test
 * Plan rows "old-client re-put after backfill" and "new-client re-put of a backfilled note".
 *
 * <p>The walk, six steps. (1) Migrate to just before {@code note-uri-001-1} as a NOBYPASSRLS owner and seed, as
 * superuser (the API canonicalizes, so it cannot write {@code chroma://}): a {@code chroma://<c>/<title>} note,
 * its aspect row (a {@code source_path} that is not the title) and highlight row, an
 * {@code nxexp://<T>/chroma://<c>/<t>} export row, and a {@code chroma://code__...} residue row. (2) Finish the
 * migration, which runs the backfill. (3) Re-put the note through {@code POST /v1/catalog/doc/register} in both
 * spellings and through {@code POST /v1/catalog/register}: one live Document, stored
 * {@code nx-note://<c>/<title>}, {@code created=false} both times, and every read route returns
 * {@code nx-note://}. (4) The aspect row is {@code nx-note://<c>/<source_path>} by its own derivation (never
 * equal to the catalog URI) and the highlight row was rewritten. (5) Re-registering the export row through
 * {@code POST /v1/catalog/import/document} in its {@code chroma://} spelling matches the backfilled row. (6)
 * {@code POST /v1/catalog/collections/rename} moves the note, its aspect and highlight, and the {@code code__}
 * residue row, each in its own spelling.
 *
 * <p>Not driven: the {@code vectors} routes' {@code include_source_uri}. They return the catalog's stored column
 * through {@code PgVectorRepository.ownersByChash} (the first-owner lookup that replaced sourceUrisByChash) and take no URI, and seeding a chunk needs the embedding
 * backend; the stored value they join to is asserted by SQL below.
 *
 * <p>Vacuity control: {@link #theSameWalkIsRedWhenTheBackfillIsSkipped} runs the identical walk with
 * {@code note-uri-001-1} marked as run but not executed, and requires it to fail at the step 3
 * {@code created=false} assertion. Without the backfill the legacy-spelled row is invisible to the canonical
 * lookup, so the re-put mints a second Document.
 */
@Tag("integration")
class NoteUriMvvIntegrationTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String BACKFILL = "note-uri-001-1";
    private static final String TOKEN = "note-uri-mvv-token-5c2";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private static final String C = "knowledge__mvv-src";
    private static final String C_NEW = "knowledge__mvv-tgt";
    private static final String IMP = "knowledge__mvv-imp";
    private static final String CODE = "code__mvv-src";
    private static final String CODE_NEW = "code__mvv-tgt";

    private static final String TITLE = "MVV Note";
    private static final String SOURCE_PATH = "mvv-aspect-path";
    private static final String EXPORTED = "mvv-exported";
    private static final String RESIDUE_PATH = "src/Mvv.java";

    private static final String NOTE_TUMBLER = "1.1.1";
    private static final String EXPORT_TUMBLER = "1.1.2";
    private static final String RESIDUE_TUMBLER = "1.1.3";

    private static final String NOTE_URI = "nx-note://" + C + "/" + TITLE;
    private static final String LEGACY_URI = "chroma://" + C + "/" + TITLE;
    private static final String ASPECT_URI = "nx-note://" + C + "/" + SOURCE_PATH;
    private static final String EXPORT_LEGACY = "nxexp://" + IMP + "/chroma://" + C + "/" + EXPORTED;
    private static final String EXPORT_NOTE = "nxexp://" + IMP + "/nx-note://" + C + "/" + EXPORTED;
    private static final String RESIDUE_URI = "chroma://" + CODE + "/" + RESIDUE_PATH;

    /** The step 3 assertion the skipped-backfill variant must fail at. */
    private static final String STEP_3_CREATED_FALSE = "created=false";

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void backfilledNoteIsOneDocumentOnEveryRouteAndFollowsTheRename() throws Exception {
        walk(true, "nexus_admin_noteuri_mvv_a", "svc_noteuri_mvv_a");
    }

    @Test
    void theSameWalkIsRedWhenTheBackfillIsSkipped() {
        assertThatThrownBy(() -> walk(false, "nexus_admin_noteuri_mvv_b", "svc_noteuri_mvv_b"))
            .as("with note-uri-001-1 marked run but not executed, the walk must fail at the step 3 assertion")
            .isInstanceOf(AssertionError.class)
            .hasMessageContaining(STEP_3_CREATED_FALSE);
    }

    /** Roles are cluster-wide, so each variant takes its own (literal names: TestRoleUniquenessScanTest). */
    private void walk(boolean runBackfill, String adminRole, String svcRole) throws Exception {
        final String adminPass = adminRole + "_pass";
        final String svcPass = svcRole + "_pass";
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        HikariDataSource adminDs = null;
        HikariDataSource svcDs = null;
        NexusService service = null;
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, adminRole, adminPass);
            adminDs = pool(pg, adminRole, adminPass, 2, "nexus-admin-" + adminRole);

            // ── 1. migrate to just before the backfill; seed what the API cannot write ──
            migrateUpTo(adminDs, BACKFILL);
            seed(pg);
            assertThat(noteRows(pg)).as("non-vacuity: the seed holds the legacy spellings before the backfill")
                .containsEntry(NOTE_TUMBLER, LEGACY_URI)
                .containsEntry(EXPORT_TUMBLER, EXPORT_LEGACY)
                .containsEntry(RESIDUE_TUMBLER, RESIDUE_URI);
            assertThat(aspectUri(pg)).isEqualTo("chroma://" + C + "/" + SOURCE_PATH);
            assertThat(highlightUri(pg)).isEqualTo(LEGACY_URI);

            // ── 2. the backfill (or, for the control, the same migration with it marked run, not executed) ──
            if (runBackfill) {
                applyRemainingChangelog(adminDs);
            } else {
                skipBackfillThenFinish(adminDs);
                assertThat(noteRows(pg)).as("the control really skipped the backfill")
                    .containsEntry(NOTE_TUMBLER, LEGACY_URI);
            }

            try (Connection su = pg.createConnection("")) {
                // the service-role bootstrap is a test changelog that needs the nexus_test objects, which
                // applyProductSchema installs and this partial walk has not
                PgContainerHelper.installTestObjectsAfterProductSchema(su);
                PgContainerHelper.bootstrapServiceRole(su, svcRole, svcPass);
                PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "note-uri-mvv");
            }
            svcDs = pool(pg, svcRole, svcPass, 4, "svc-" + svcRole);
            service = new NexusService(0, TOKEN, svcDs);
            service.start();
            Http h = new Http(service.getPort());
            // Burn the once-per-process ghost sweep before registering anything
            // (see CatalogRenameIdentityCascadeTest#startAll).
            h.get("/v1/catalog/collections/list");

            // ── 3. re-put the note in both spellings, through both register routes ──
            long liveBefore = liveDocuments(pg);
            for (String spelling : List.of(LEGACY_URI, NOTE_URI)) {
                Map<String, Object> out = h.ok(h.post("/v1/catalog/doc/register", docBody(spelling)));
                assertThat(out.get("created"))
                    .as("re-put of the backfilled note as '%s' returns " + STEP_3_CREATED_FALSE, spelling)
                    .isEqualTo(false);
                assertThat(out.get("tumbler")).as("re-put as '%s' returns the one Document", spelling)
                    .isEqualTo(NOTE_TUMBLER);
            }
            // /register addresses by tumbler: the legacy spelling at the note's own tumbler converges on the
            // stored nx-note:// value, and at any other tumbler it is refused as an identity conflict.
            h.ok(h.post("/v1/catalog/register", registerBody(NOTE_TUMBLER, LEGACY_URI)));
            h.ok(h.post("/v1/catalog/register", registerBody(NOTE_TUMBLER, NOTE_URI)));
            for (String spelling : List.of(LEGACY_URI, NOTE_URI)) {
                var refused = h.post("/v1/catalog/register", registerBody("1.1.98", spelling));
                assertThat(refused.statusCode()).as("/register of '%s' at a second tumbler is refused: %s", spelling,
                    refused.body()).isEqualTo(409);
            }

            assertThat(liveDocuments(pg)).as("no re-put minted a Document").isEqualTo(liveBefore);
            assertThat(liveNoteDocuments(pg, C)).as("exactly one live Document holds the note, in either spelling")
                .containsExactly(NOTE_TUMBLER);
            assertThat(noteRows(pg)).as("stored source_uri is nx-note://<c>/<title>")
                .containsEntry(NOTE_TUMBLER, NOTE_URI);
            assertThat(noteRows(pg)).as("no row at the refused tumbler").doesNotContainKey("1.1.98");

            int examined = 0;
            examined += assertEveryReadRouteSpeaksNxNote(h, "after the re-puts");

            // ── 4. the aspect and highlight rows ──
            assertThat(aspectUri(pg)).as("aspect row: its own derivation from source_path").isEqualTo(ASPECT_URI);
            assertThat(aspectUri(pg)).as("the aspect URI is not the catalog URI (source_path is not the title)")
                .isNotEqualTo(noteRows(pg).get(NOTE_TUMBLER));
            assertThat(highlightUri(pg)).as("highlight row was rewritten").isEqualTo(NOTE_URI);

            // Enumeration by the schema: after backfill and re-puts only the residue row holds chroma://.
            assertThat(columnsHolding(pg, "chroma://")).as("only catalog_documents.source_uri holds chroma://")
                .containsExactly("catalog_documents.source_uri");
            assertThat(rowsHolding(pg, "chroma://")).as("and only the residue row").containsExactly(RESIDUE_TUMBLER);

            // ── 5. the export row, re-registered through the import route in its chroma:// spelling ──
            long liveBeforeImport = liveDocuments(pg);
            assertThat(h.ok(h.post("/v1/catalog/import/document", importBody(EXPORT_TUMBLER, EXPORT_LEGACY)))
                .get("imported")).isEqualTo(1);
            assertThat(liveDocuments(pg)).as("the import minted no Document").isEqualTo(liveBeforeImport);
            assertThat(noteRows(pg)).as("the import matched the backfilled export row and kept its spelling")
                .containsEntry(EXPORT_TUMBLER, EXPORT_NOTE);
            var importDup = h.post("/v1/catalog/import/document", importBody("1.1.97", EXPORT_LEGACY));
            assertThat(importDup.statusCode()).as("the import at a second tumbler is refused: %s", importDup.body())
                .isEqualTo(409);
            assertThat(noteRows(pg)).as("no duplicate export row").doesNotContainKey("1.1.97");

            // ── 6. rename the note's collection and the code__ collection ──
            assertThat(h.post("/v1/catalog/collections/rename", Map.of("old_name", C, "new_name", C_NEW)).statusCode())
                .isEqualTo(200);
            assertThat(h.post("/v1/catalog/collections/rename", Map.of("old_name", CODE, "new_name", CODE_NEW))
                .statusCode()).isEqualTo(200);
            Map<String, String> after = noteRows(pg);
            assertThat(after.get(NOTE_TUMBLER)).as("the note follows in its own spelling")
                .isEqualTo("nx-note://" + C_NEW + "/" + TITLE);
            assertThat(after.get(RESIDUE_TUMBLER)).as("the residue row follows in its own spelling")
                .isEqualTo("chroma://" + CODE_NEW + "/" + RESIDUE_PATH);
            assertThat(after.get(EXPORT_TUMBLER)).as("an nxexp:// identity is not a collection URI and is not moved")
                .isEqualTo(EXPORT_NOTE);
            assertThat(aspectUri(pg)).as("the aspect follows the rename")
                .isEqualTo("nx-note://" + C_NEW + "/" + SOURCE_PATH);
            assertThat(highlightUri(pg)).as("the highlight follows the rename")
                .isEqualTo("nx-note://" + C_NEW + "/" + TITLE);
            assertThat(columnsHolding(pg, "chroma://" + CODE + "/")).as("nothing names the old code collection")
                .isEmpty();
            assertThat(columnsHolding(pg, "nx-note://" + C + "/")).as("only the export identity still names the old note collection")
                .containsExactly("catalog_documents.source_uri");

            // The renamed note is still found by a re-put in either spelling under its new collection.
            long liveBeforeRename = liveDocuments(pg);
            for (String spelling : List.of("chroma://" + C_NEW + "/" + TITLE, "nx-note://" + C_NEW + "/" + TITLE)) {
                Map<String, Object> out = h.ok(h.post("/v1/catalog/doc/register", docBody(spelling, C_NEW)));
                assertThat(out.get("created")).as("post-rename re-put as '%s'", spelling).isEqualTo(false);
                assertThat(out.get("tumbler")).isEqualTo(NOTE_TUMBLER);
            }
            assertThat(liveDocuments(pg)).as("the post-rename re-puts minted no Document").isEqualTo(liveBeforeRename);

            assertThat(examined).as("read-route rows examined for nx-note://").isGreaterThanOrEqualTo(13);
        } finally {
            if (service != null) service.stop();
            if (svcDs != null) svcDs.close();
            if (adminDs != null) adminDs.close();
            pg.stop();
        }
    }

    /**
     * Every route that returns the note's URI, driven with each spelling where it takes one. Returns the number
     * of URI values examined, so a route that answered with nothing cannot pass.
     */
    private int assertEveryReadRouteSpeaksNxNote(Http h, String stage) throws Exception {
        int examined = 0;
        String legacyQ = enc(LEGACY_URI);
        String noteQ = enc(NOTE_URI);
        String collQ = enc(C);
        examined += expectUris(h, stage, "/v1/catalog/list?source_uri=" + legacyQ, NOTE_URI);
        examined += expectUris(h, stage, "/v1/catalog/list?source_uri=" + noteQ, NOTE_URI);
        examined += expectUris(h, stage, "/v1/catalog/resolve?source_uri=" + legacyQ, NOTE_URI);
        examined += expectUris(h, stage, "/v1/catalog/resolve?source_uri=" + noteQ, NOTE_URI);
        examined += expectUris(h, stage, "/v1/catalog/show?tumbler=" + NOTE_TUMBLER, NOTE_URI);
        examined += expectUris(h, stage, "/v1/catalog/list?collection=" + collQ, NOTE_URI);
        examined += expectUris(h, stage, "/v1/aspects/get?collection=" + collQ + "&source_path=" + enc(SOURCE_PATH),
            ASPECT_URI);
        examined += expectUris(h, stage, "/v1/aspects/get_by_doc_id?doc_id=" + NOTE_TUMBLER, ASPECT_URI);
        examined += expectUris(h, stage, "/v1/aspects/list_by_collection?collection=" + collQ, ASPECT_URI);
        examined += expectUris(h, stage, "/v1/aspects/highlights/get?doc_id=" + NOTE_TUMBLER, NOTE_URI);
        examined += expectUris(h, stage, "/v1/aspects/highlights/get_by_source_uri?source_uri=" + legacyQ, NOTE_URI);
        examined += expectUris(h, stage, "/v1/aspects/highlights/get_by_source_uri?source_uri=" + noteQ, NOTE_URI);
        examined += expectUris(h, stage, "/v1/aspects/highlights/list", NOTE_URI);
        return examined;
    }

    /** GET {@code path} and require every {@code source_uri} it carries to be {@code expected}, at least once. */
    private int expectUris(Http h, String stage, String path, String expected) throws Exception {
        JsonNode body = mapper.readTree(h.get(path).body());
        List<String> uris = new ArrayList<>();
        collectSourceUris(body, uris);
        assertThat(uris).as("%s: GET %s carries source_uri values", stage, path).isNotEmpty();
        assertThat(uris).as("%s: GET %s returns only %s", stage, path, expected).containsOnly(expected);
        return uris.size();
    }

    private static void collectSourceUris(JsonNode node, List<String> out) {
        if (node.isArray()) {
            node.forEach(n -> collectSourceUris(n, out));
        } else if (node.isObject()) {
            if (node.has("source_uri")) {
                out.add(node.get("source_uri").asText());
            } else if (node.has("documents")) {
                collectSourceUris(node.get("documents"), out);
            }
        }
    }

    // ── bodies ─────────────────────────────────────────────────────────────────

    private static Map<String, Object> docBody(String uri) {
        return docBody(uri, C);
    }

    private static Map<String, Object> docBody(String uri, String collection) {
        var b = new LinkedHashMap<String, Object>();
        b.put("owner_prefix", "1.1");
        b.put("title", TITLE);
        b.put("content_type", "knowledge");
        b.put("physical_collection", collection);
        b.put("source_uri", uri);
        return b;
    }

    private static Map<String, Object> registerBody(String tumbler, String uri) {
        var b = new LinkedHashMap<String, Object>();
        b.put("tumbler", tumbler);
        b.put("title", TITLE);
        b.put("content_type", "knowledge");
        b.put("physical_collection", C);
        b.put("source_uri", uri);
        return b;
    }

    private static Map<String, Object> importBody(String tumbler, String uri) {
        var b = new LinkedHashMap<String, Object>();
        b.put("tumbler", tumbler);
        b.put("title", EXPORTED);
        b.put("content_type", "knowledge");
        b.put("physical_collection", IMP);
        b.put("source_uri", uri);
        return b;
    }

    // ── seed and probes (superuser) ────────────────────────────────────────────

    private static void seed(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext c = DSL.using(su, SQLDialect.POSTGRES);
            for (String coll : List.of(C, IMP, CODE)) {
                PgContainerHelper.insertCollection(c, TENANT, coll);
            }
            insertDoc(c, NOTE_TUMBLER, TITLE, C, LEGACY_URI);
            insertDoc(c, EXPORT_TUMBLER, EXPORTED, IMP, EXPORT_LEGACY);
            insertDoc(c, RESIDUE_TUMBLER, "Mvv.java", CODE, RESIDUE_URI);
            c.insertInto(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.TENANT_ID, TENANT)
                .set(DOCUMENT_ASPECTS.COLLECTION, C)
                .set(DOCUMENT_ASPECTS.SOURCE_PATH, SOURCE_PATH)
                .set(DOCUMENT_ASPECTS.PROPOSED_METHOD, "note-uri mvv")
                .set(DOCUMENT_ASPECTS.EXTRACTED_AT, DSL.currentOffsetDateTime())
                .set(DOCUMENT_ASPECTS.MODEL_VERSION, "test-model")
                .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "test-extractor")
                .set(DOCUMENT_ASPECTS.SOURCE_URI, "chroma://" + C + "/" + SOURCE_PATH)
                .set(DOCUMENT_ASPECTS.DOC_ID, NOTE_TUMBLER)
                .execute();
            c.insertInto(DOCUMENT_HIGHLIGHTS)
                .set(DOCUMENT_HIGHLIGHTS.TENANT_ID, TENANT)
                .set(DOCUMENT_HIGHLIGHTS.DOC_ID, NOTE_TUMBLER)
                .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, LEGACY_URI)
                .set(DOCUMENT_HIGHLIGHTS.COLLECTION, C)
                .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT, DSL.currentOffsetDateTime())
                .execute();
        }
    }

    private static void insertDoc(DSLContext c, String tumbler, String title, String collection, String uri) {
        c.insertInto(CATALOG_DOCUMENTS,
                CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SOURCE_URI)
            .values(TENANT, tumbler, title, "knowledge", collection, uri)
            .execute();
    }

    private static <T> T query(PostgreSQLContainer<?> pg, Function<DSLContext, T> body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return body.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    /** {@code tumbler -> source_uri} of the live catalog rows. */
    private static Map<String, String> noteRows(PostgreSQLContainer<?> pg) throws Exception {
        return query(pg, c -> {
            Map<String, String> out = new LinkedHashMap<>();
            c.select(CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
                .orderBy(CATALOG_DOCUMENTS.TUMBLER)
                .forEach(r -> out.put(r.value1(), r.value2()));
            return out;
        });
    }

    private static long liveDocuments(PostgreSQLContainer<?> pg) throws Exception {
        return query(pg, c -> c.fetchCount(CATALOG_DOCUMENTS,
            CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())));
    }

    /** Tumblers of live rows whose URI is the note's, in either spelling, under collection {@code coll}. */
    private static List<String> liveNoteDocuments(PostgreSQLContainer<?> pg, String coll) throws Exception {
        return query(pg, c -> c.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
            .and(CATALOG_DOCUMENTS.SOURCE_URI.in("nx-note://" + coll + "/" + TITLE, "chroma://" + coll + "/" + TITLE))
            .fetch(CATALOG_DOCUMENTS.TUMBLER));
    }

    private static String aspectUri(PostgreSQLContainer<?> pg) throws Exception {
        return query(pg, c -> c.select(DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_ASPECTS.DOC_ID.eq(NOTE_TUMBLER))
            .fetchOne(DOCUMENT_ASPECTS.SOURCE_URI));
    }

    private static String highlightUri(PostgreSQLContainer<?> pg) throws Exception {
        return query(pg, c -> c.select(DOCUMENT_HIGHLIGHTS.SOURCE_URI).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(NOTE_TUMBLER))
            .fetchOne(DOCUMENT_HIGHLIGHTS.SOURCE_URI));
    }

    /** Tumblers of live catalog rows whose source_uri contains {@code needle}. */
    private static List<String> rowsHolding(PostgreSQLContainer<?> pg, String needle) throws Exception {
        return query(pg, c -> c.select(CATALOG_DOCUMENTS.TUMBLER).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.DELETED_AT.isNull())
            .and(DSL.function("strpos", Integer.class, CATALOG_DOCUMENTS.SOURCE_URI, DSL.val(needle)).gt(0))
            .orderBy(CATALOG_DOCUMENTS.TUMBLER)
            .fetch(CATALOG_DOCUMENTS.TUMBLER));
    }

    /** Every nexus.* base-table text/varchar/json/jsonb column holding {@code needle}, as "table.column". */
    private static List<String> columnsHolding(PostgreSQLContainer<?> pg, String needle) throws Exception {
        return query(pg, dsl -> {
            var columns = DSL.table(DSL.name("information_schema", "columns")).as("c");
            var tables = DSL.table(DSL.name("information_schema", "tables")).as("t");
            Field<String> cSchema = DSL.field(DSL.name("c", "table_schema"), String.class);
            Field<String> cTable = DSL.field(DSL.name("c", "table_name"), String.class);
            Field<String> cColumn = DSL.field(DSL.name("c", "column_name"), String.class);
            Field<String> cType = DSL.field(DSL.name("c", "data_type"), String.class);
            Field<String> tSchema = DSL.field(DSL.name("t", "table_schema"), String.class);
            Field<String> tTable = DSL.field(DSL.name("t", "table_name"), String.class);
            Field<String> tType = DSL.field(DSL.name("t", "table_type"), String.class);
            var cols = dsl.select(cTable, cColumn).from(columns)
                .join(tables).on(tSchema.eq(cSchema).and(tTable.eq(cTable)).and(tType.eq("BASE TABLE")))
                .where(cSchema.eq("nexus"))
                .and(cType.in("text", "character varying", "jsonb", "json"))
                .fetch();
            assertThat(cols).as("the sweep must see the URI columns it is checking")
                .anySatisfy(r -> assertThat(r.value2()).isEqualTo("source_uri"));
            List<String> hits = new ArrayList<>();
            for (var col : cols) {
                Field<String> asText = DSL.field(DSL.name(col.value2())).cast(String.class);
                boolean holds = dsl.fetchExists(dsl.selectOne()
                    .from(DSL.table(DSL.name("nexus", col.value1())))
                    .where(DSL.function("strpos", Integer.class, asText, DSL.val(needle)).gt(0)));
                if (holds) hits.add(col.value1() + "." + col.value2());
            }
            return hits;
        });
    }

    // ── migration ──────────────────────────────────────────────────────────────

    private static HikariDataSource pool(PostgreSQLContainer<?> pg, String user, String pass, int size, String name) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(size);
        cfg.setPoolName(name);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
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

    /** The control: the backfill is recorded as run without executing, then the rest of the migration runs. */
    private static void skipBackfillThenFinish(HikariDataSource adminDs) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                // runAlways changesets are always "unrun", so the backfill is found by id, not by position
                ChangeSet backfill = unrun.stream().filter(cs -> BACKFILL.equals(cs.getId())).findFirst()
                    .orElseThrow(() -> new AssertionError(BACKFILL + " must be pending"));
                var history = ChangeLogHistoryServiceFactory.getInstance().getChangeLogService(database);
                history.init();
                history.setExecType(backfill, ChangeSet.ExecType.MARK_RAN);
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
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
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

    // ── HTTP ───────────────────────────────────────────────────────────────────

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    /** The service on {@code 127.0.0.1:port}, the default tenant, JSON bodies. */
    private final class Http {
        private final HttpClient client = TestHttp.client();
        private final String base;

        Http(int port) {
            this.base = "http://127.0.0.1:" + port;
        }

        HttpResponse<String> get(String path) throws Exception {
            return client.send(TestHttp.request(base + path)
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .GET().build(), HttpResponse.BodyHandlers.ofString());
        }

        HttpResponse<String> post(String path, Map<String, Object> body) throws Exception {
            return client.send(TestHttp.request(base + path)
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
                .build(), HttpResponse.BodyHandlers.ofString());
        }

        /** The response body as a map, after requiring a 200. */
        @SuppressWarnings("unchecked")
        Map<String, Object> ok(HttpResponse<String> resp) throws Exception {
            assertThat(resp.statusCode()).as("response: %s", resp.body()).isEqualTo(200);
            return mapper.readValue(resp.body(), Map.class);
        }
    }
}
