// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-sis0m.3 (shakeout 7.64.1 Surface F F9): a rename A-&gt;B left B's catalog row with
 * A's owner (for a knowledge collection, A's subject) and left every document's
 * {@code chroma://A/<title>} source_uri naming A. A re-put of the same title into B then
 * missed its own document (the identity is the URI) and minted a duplicate.
 *
 * <p>The rename now applies the new row's content_type/owner_id when the client sends them
 * (derived from the new name client-side; the engine does not parse names, RDR-204), and
 * rewrites the {@code chroma://A/} prefix to {@code chroma://B/} in the same transaction,
 * refusing with 409 when the rewritten URI already names a live document elsewhere.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogRenameIdentityCascadeTest {

    private static final String TOKEN = "catalog-rename-identity-token-9f1";
    private static final String SVC_ROLE = "svc_cat_ren_ident";
    private static final String SVC_PASS = "svc_cat_ren_ident_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = TestHttp.client();
        // Burn the once-per-process ghost sweep before registering anything
        // (see CatalogHandlerRenameTest#startAll).
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── the new row's content_type / owner_id ──────────────────────────────────

    @Test
    void clientSuppliedAttributesAreTheNewRows() throws Exception {
        seedCollection("knowledge__alpha-subj__voyage-context-3__v1", "knowledge", "alpha-subj");
        seedDocument("knowledge__alpha-subj__voyage-context-3__v1", "own4-doc", "Own4");
        var body = new LinkedHashMap<String, Object>();
        body.put("content_type", "knowledge");
        body.put("owner_id", "beta-subj");
        assertThat(rename("knowledge__alpha-subj__voyage-context-3__v1",
            "knowledge__beta-subj__voyage-context-3__v1", body).statusCode()).isEqualTo(200);
        var row = collectionRow("knowledge__beta-subj__voyage-context-3__v1");
        assertThat(row.get("owner_id")).isEqualTo("beta-subj");
        assertThat(row.get("content_type")).isEqualTo("knowledge");
    }

    @Test
    void clientSuppliedAttributesAlsoApplyWhenRevivingATombstone() throws Exception {
        // B->A onto A's own rename tombstone takes the upsert's DO UPDATE arm, not the insert.
        seedCollection("knowledge__rv-a", "knowledge", "rv-a");
        seedDocument("knowledge__rv-a", "rv-doc", "Revive");
        assertThat(rename("knowledge__rv-a", "knowledge__rv-b",
            Map.of("content_type", "knowledge", "owner_id", "rv-b")).statusCode()).isEqualTo(200);
        assertThat(rename("knowledge__rv-b", "knowledge__rv-a",
            Map.of("content_type", "knowledge", "owner_id", "rv-a-revived")).statusCode()).isEqualTo(200);
        var row = collectionRow("knowledge__rv-a");
        assertThat(row.get("superseded_by")).isEqualTo("");
        assertThat(row.get("owner_id")).isEqualTo("rv-a-revived");
    }

    @Test
    void absentAttributesKeepTheSources() throws Exception {
        // A client predating the field sends neither; the row copies the source's, as before.
        seedCollection("hren__keep-src", "knowledge", "kept-owner");
        seedDocument("hren__keep-src", "keep-doc", "Keep");
        assertThat(rename("hren__keep-src", "hren__keep-tgt", Map.of()).statusCode()).isEqualTo(200);
        var row = collectionRow("hren__keep-tgt");
        assertThat(row.get("owner_id")).isEqualTo("kept-owner");
        assertThat(row.get("content_type")).isEqualTo("knowledge");
    }

    @Test
    void aBlankAttributeIsRefusedAndMovesNothing() throws Exception {
        seedCollection("hren__blank-src", "knowledge", "blank-owner");
        seedDocument("hren__blank-src", "blank-doc", "Blank");
        var resp = rename("hren__blank-src", "hren__blank-tgt", Map.of("owner_id", " "));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("owner_id");
        assertThat(collectionRow("hren__blank-tgt")).isNull();
        assertThat(collectionRow("hren__blank-src").get("superseded_by")).isEqualTo("");
    }

    // ── chroma:// source_uri rewrite ───────────────────────────────────────────

    @Test
    void everyStoredChromaUriFollowsTheRename() throws Exception {
        String a = "knowledge__uri-src__voyage-context-3__v1";
        String b = "knowledge__uri-tgt__voyage-context-3__v1";
        seedCollection(a, "knowledge", "uri-src");
        seedDocument(a, "uri-doc", "Note One");
        setSourceUri("uri-doc", "chroma://" + a + "/Note%20One");
        withDsl(dsl -> dsl.insertInto(DOCUMENT_ASPECTS,
                DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION, DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                DOCUMENT_ASPECTS.SOURCE_URI, DOCUMENT_ASPECTS.DOC_ID)
            .values(DSL.val(TENANT), DSL.val(a), DSL.val("Note One"), DSL.currentOffsetDateTime(),
                DSL.val("m"), DSL.val("e"), DSL.val("chroma://" + a + "/Note%20One"), DSL.val("uri-doc"))
            .execute());
        withDsl(dsl -> dsl.insertInto(DOCUMENT_HIGHLIGHTS,
                DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.SOURCE_URI,
                DOCUMENT_HIGHLIGHTS.COLLECTION, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
            .values(DSL.val(TENANT), DSL.val("uri-doc"), DSL.val("chroma://" + a + "/Note%20One"),
                DSL.val(a), DSL.currentOffsetDateTime())
            .execute());

        assertThat(rename(a, b, Map.of()).statusCode()).isEqualTo(200);

        assertThat(docSourceUri("uri-doc")).isEqualTo("chroma://" + b + "/Note%20One");
        assertThat(queryString(dsl -> dsl.select(DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.DOC_ID.eq("uri-doc")).fetchOne(DOCUMENT_ASPECTS.SOURCE_URI)))
            .isEqualTo("chroma://" + b + "/Note%20One");
        assertThat(queryString(dsl -> dsl.select(DOCUMENT_HIGHLIGHTS.SOURCE_URI).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("uri-doc")).fetchOne(DOCUMENT_HIGHLIGHTS.SOURCE_URI)))
            .isEqualTo("chroma://" + b + "/Note%20One");
        // Enumeration by the schema, not by a list someone keeps: no text or json column
        // anywhere in nexus still names the old collection's URI prefix.
        assertThat(columnsHolding("chroma://" + a + "/")).isEmpty();
    }

    @Test
    void theRewriteIsAPrefixMatchNotALikePattern() throws Exception {
        // '_' is a LIKE wildcard: chroma://knowledge__u_x/% would also match knowledge__uax.
        seedCollection("knowledge__u_x", "knowledge", "u_x");
        seedCollection("knowledge__uax", "knowledge", "uax");
        seedDocument("knowledge__u_x", "us-doc", "T");
        setSourceUri("us-doc", "chroma://knowledge__u_x/T");
        seedDocument("knowledge__uax", "ua-doc", "T");
        setSourceUri("ua-doc", "chroma://knowledge__uax/T");

        assertThat(rename("knowledge__u_x", "knowledge__u_y", Map.of()).statusCode()).isEqualTo(200);

        assertThat(docSourceUri("ua-doc")).isEqualTo("chroma://knowledge__uax/T");
        assertThat(docSourceUri("us-doc")).isEqualTo("chroma://knowledge__u_y/T");
    }

    @Test
    void aRewriteOntoALiveUriIsRefusedAndMovesNothing() throws Exception {
        String a = "knowledge__col-src";
        String b = "knowledge__col-tgt";
        seedCollection(a, "knowledge", "col-src");
        seedCollection("knowledge__col-other", "knowledge", "col-other");
        seedDocument(a, "col-doc", "Dup");
        setSourceUri("col-doc", "chroma://" + a + "/Dup");
        // A live document elsewhere already carries the URI the rewrite would produce.
        seedDocument("knowledge__col-other", "col-squat", "Dup");
        setSourceUri("col-squat", "chroma://" + b + "/Dup");

        var resp = rename(a, b, Map.of());
        assertThat(resp.statusCode()).isEqualTo(409);
        assertThat(resp.body()).contains("chroma://" + b + "/Dup").contains("col-squat");

        assertThat(docSourceUri("col-doc")).isEqualTo("chroma://" + a + "/Dup");
        assertThat(queryString(dsl -> dsl.select(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TUMBLER.eq("col-doc")).fetchOne(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)))
            .isEqualTo(a);
        assertThat(collectionRow(a).get("superseded_by")).isEqualTo("");
        assertThat(collectionRow(b)).isNull();
    }

    @Test
    void aStaleUriOnAForeignDocumentIsNotThisRenamesToTouch() throws Exception {
        // Critique of a11876053: a document in a THIRD collection still carrying a stale
        // chroma://<name>/ URI (a pre-fix rename, then the name reused) must be neither
        // rewritten nor reported as a collision by a rename of the new <name>.
        String reused = "knowledge__reuse-x";
        seedCollection(reused, "knowledge", "reuse-x");
        seedCollection("knowledge__third", "knowledge", "third");
        seedDocument(reused, "member-doc", "Member");
        setSourceUri("member-doc", "chroma://" + reused + "/Member");
        seedDocument("knowledge__third", "orphan-doc", "Orphan");
        setSourceUri("orphan-doc", "chroma://" + reused + "/Orphan");
        // Its rewritten form collides with a live document; a text-only check would 409.
        seedDocument("knowledge__third", "squat-doc", "Squat");
        setSourceUri("squat-doc", "chroma://knowledge__reuse-y/Orphan");

        assertThat(rename(reused, "knowledge__reuse-y", Map.of()).statusCode()).isEqualTo(200);
        assertThat(docSourceUri("member-doc")).isEqualTo("chroma://knowledge__reuse-y/Member");
        assertThat(docSourceUri("orphan-doc")).isEqualTo("chroma://" + reused + "/Orphan");
    }

    @Test
    void aRoundTripRestoresTheOriginalUris() throws Exception {
        String a = "knowledge__rt-src";
        String b = "knowledge__rt-tgt";
        seedCollection(a, "knowledge", "rt-src");
        seedDocument(a, "rt-doc", "Round");
        setSourceUri("rt-doc", "chroma://" + a + "/Round");
        assertThat(rename(a, b, Map.of()).statusCode()).isEqualTo(200);
        assertThat(docSourceUri("rt-doc")).isEqualTo("chroma://" + b + "/Round");
        assertThat(rename(b, a, Map.of()).statusCode()).isEqualTo(200);
        assertThat(docSourceUri("rt-doc")).isEqualTo("chroma://" + a + "/Round");
    }

    // ── owner-root: hyphenated owner_id against a dotted owner tumbler ─────────

    @Test
    void ownerRootResolvesAHyphenatedOwnerIdToItsDottedOwner() throws Exception {
        // A code collection's owner_id is the owner tumbler with dots as hyphens (the name
        // segment charset); catalog_owners keys on the dotted tumbler. The join compared
        // them verbatim, so repo_root was always '' and reindex fell back to repos.json.
        withDsl(dsl -> dsl.insertInto(CATALOG_OWNERS,
                CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME,
                CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_ROOT)
            .values(TENANT, "1.77", "owner-root-repo", "repo", "/src/owner-root-repo")
            .execute());
        seedCollection("code__1-77__voyage-code-3__v1", "code", "1-77");
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort()
                + "/v1/catalog/collections/owner-root?name=code__1-77__voyage-code-3__v1")
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("owner_id")).isEqualTo("1-77");
        assertThat(body.get("repo_root")).isEqualTo("/src/owner-root-repo");
    }

    // ── helpers ────────────────────────────────────────────────────────────────

    private HttpResponse<String> rename(String oldName, String newName, Map<String, Object> extra) throws Exception {
        var body = new LinkedHashMap<String, Object>(extra);
        body.put("old_name", oldName);
        body.put("new_name", newName);
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/rename")
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private Map<String, Object> collectionRow(String name) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort()
                + "/v1/catalog/collections/get?name=" + name)
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var r = http.send(req, HttpResponse.BodyHandlers.ofString());
        return r.statusCode() == 200 ? mapper.readValue(r.body(), MAP_T) : null;
    }

    private void withDsl(Consumer<DSLContext> body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            body.accept(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private <T> T query(Function<DSLContext, T> body) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return body.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private String queryString(Function<DSLContext, String> body) throws Exception {
        return query(body);
    }

    private void seedCollection(String name, String contentType, String ownerId) throws Exception {
        withDsl(dsl -> {
            PgContainerHelper.insertCollection(dsl, TENANT, name);
            dsl.update(CATALOG_COLLECTIONS)
                .set(CATALOG_COLLECTIONS.CONTENT_TYPE, contentType)
                .set(CATALOG_COLLECTIONS.OWNER_ID, ownerId)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(name)))
                .execute();
        });
    }

    private void seedDocument(String collection, String tumbler, String title) throws Exception {
        withDsl(dsl -> dsl.insertInto(CATALOG_DOCUMENTS,
                CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
            .values(TENANT, tumbler, title, collection)
            .execute());
    }

    private void setSourceUri(String tumbler, String uri) throws Exception {
        withDsl(dsl -> dsl.update(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, uri)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)))
            .execute());
    }

    private String docSourceUri(String tumbler) throws Exception {
        return query(dsl -> dsl.select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
    }

    /** Every nexus.* base-table text/varchar/json/jsonb column holding {@code needle}, as "table.column". */
    private List<String> columnsHolding(String needle) throws Exception {
        return query(dsl -> {
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
}
