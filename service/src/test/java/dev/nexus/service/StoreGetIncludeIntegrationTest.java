// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd (engine half of the client's c49f53ff1): {@code POST /v1/vectors/store-get} reads an optional
 * {@code include} array and projects to it. Over real HTTP against real PG.
 *
 * <p>Absent {@code include} is the full envelope, unchanged. Present, the statement selects only what the
 * caller reads: {@code include=["metadatas"]} returns no {@code documents} key, {@code ["documents"]} returns no
 * {@code metadatas} key, and the always-keys ({@code ids}, {@code count}, {@code chashes}, {@code spans},
 * {@code sensitives}) are the same either way. A value outside the vocabulary is a 400.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StoreGetIncludeIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN  = "f3yqd-include-token-0123456789abcdef01";
    private static final String TENANT = "f3yqd-include-tenant";
    private static final String COL    = "knowledge__f3yqdinc__voyage-context-3__v1";
    private static final String COL_S  = "knowledge__f3yqdsens__voyage-context-3__v1";
    /** One collection, three documents: X1 and X2 plain, XS flagged sensitive (the mixed case of exclude). */
    private static final String COL_X  = "knowledge__f3yqdmix__voyage-context-3__v1";

    private static final String C1 = Chash.ofText("f3yqd-inc-1").toHex();
    private static final String C2 = Chash.ofText("f3yqd-inc-2").toHex();
    private static final String C3 = Chash.ofText("f3yqd-inc-3").toHex();   // no span keys at all
    private static final String CS = Chash.ofText("f3yqd-inc-sens").toHex(); // owned by a sensitive document
    private static final String X1 = Chash.ofText("f3yqd-inc-x1").toHex();
    private static final String X2 = Chash.ofText("f3yqd-inc-x2").toHex();
    private static final String XS = Chash.ofText("f3yqd-inc-xs").toHex();
    private static final List<String> X_LIVE = List.of(X1, X2).stream().sorted().toList();
    /** store-get answers in chash order, not request order. */
    private static final List<String> ORDER = List.of(C1, C2, C3).stream().sorted().toList();
    private static final Map<String, String> TEXT = Map.of(C1, "text one", C2, "text two", C3, "text three");
    private static final String HASH_1 =
        "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899";
    private static final Map<String, String> SPAN = Map.of(C1, "chash:" + HASH_1, C2, "5-15", C3, "");
    private static final List<String> ORDERED_TEXT = ORDER.stream().map(TEXT::get).toList();
    private static final List<String> ORDERED_SPAN = ORDER.stream().map(SPAN::get).toList();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    FakeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "f3yqd-include");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        embedder = new FakeEmbedder(1024);
        var repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, null, null, repo);
        service.start();
        http = TestHttp.client();

        // Burn the tenant's first-request ghost sweep before collections are registered
        // (BridgeAddressFieldsTest.startAll documents the race).
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COL);
            PgContainerHelper.insertCollection(ctx, TENANT, COL_S);
            PgContainerHelper.insertCollection(ctx, TENANT, COL_X);
            PgContainerHelper.insertChunks(ctx, TENANT, COL, List.of(C1, C2, C3),
                List.of("text one", "text two", "text three"),
                List.of(Map.of("title", "T1", "chunk_text_hash", HASH_1, "line_start", 10, "line_end", 20,
                               "extra_key", "kept"),
                        Map.of("title", "T2", "line_start", 5, "line_end", 15),
                        Map.of("title", "T3")),
                embedder);
            PgContainerHelper.ownChunks(ctx, TENANT, COL, C1, C2, C3);
            PgContainerHelper.insertChunks(ctx, TENANT, COL_S, List.of(CS), List.of("secret text"),
                List.of(Map.of("title", "S")), embedder);
            PgContainerHelper.ownChunks(ctx, TENANT, COL_S, CS);
            ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, true)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT))
                .and(CATALOG_DOCUMENTS.TUMBLER.eq("own-" + COL_S)).execute();
            PgContainerHelper.insertChunks(ctx, TENANT, COL_X, List.of(X1, X2, XS),
                List.of("x one", "x two", "x secret"),
                List.of(Map.of("title", "X1", "line_start", 1, "line_end", 2),
                        Map.of("title", "X2", "line_start", 3, "line_end", 4),
                        Map.of("title", "XS", "line_start", 5, "line_end", 6)),
                embedder);
            ownAs(ctx, "x-doc-1", X1, false);
            ownAs(ctx, "x-doc-2", X2, false);
            ownAs(ctx, "x-doc-s", XS, true);
        }
    }

    /** A catalog document of its own that owns one chunk of {@link #COL_X}, flagged or not. */
    private static void ownAs(DSLContext ctx, String doc, String chash, boolean sensitive) {
        ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.SENSITIVE)
            .values(TENANT, doc, "Owner " + doc, COL_X, sensitive).execute();
        ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL)
            .values(TENANT, doc, 0, Chash.fromHex(chash).toBytes(), COL_X,
                    PgContainerHelper.collectionModel(ctx, TENANT, COL_X)).execute();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private HttpResponse<String> post(Map<String, Object> body) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/store-get")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> storeGet(String col, List<String> ids, Object include, boolean withInclude)
            throws Exception {
        return storeGet(col, ids, include, withInclude, Map.of());
    }

    private HttpResponse<String> storeGet(String col, List<String> ids, Object include, boolean withInclude,
                                          Map<String, Object> extra) throws Exception {
        Map<String, Object> body = new HashMap<>(extra);
        body.put("collection", col);
        body.put("ids", ids);
        if (withInclude) body.put("include", include);
        return post(body);
    }

    private Map<String, Object> ok(HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readValue(r.body(), MAP_TYPE);
    }

    private static final List<String> ALWAYS = List.of("ids", "count", "chashes", "spans", "sensitives");

    // (a) absent include = today's envelope --------------------------------------------------------------

    @Test
    void absentInclude_isTheFullEnvelope() throws Exception {
        var env = ok(storeGet(COL, List.of(C1, C2, C3), null, false));
        assertThat(env).containsKeys("ids", "documents", "metadatas", "count", "chashes", "spans", "sensitives");
        assertThat((List<Object>) env.get("documents")).containsExactlyElementsOf(ORDERED_TEXT);
        int i = ((List<Object>) env.get("ids")).indexOf(C1);
        assertThat(((List<Map<String, Object>>) env.get("metadatas")).get(i)).containsEntry("extra_key", "kept");
    }

    @Test
    void includeBothKeys_isTheSameBytesAsAbsent() throws Exception {
        var absent = storeGet(COL, List.of(C1, C2, C3), null, false);
        var both = storeGet(COL, List.of(C1, C2, C3), List.of("documents", "metadatas"), true);
        assertThat(both.body()).isEqualTo(absent.body());
    }

    // (b) metadatas only: no documents ---------------------------------------------------------------------

    @Test
    void includeMetadatas_returnsNoDocumentsKey_andFullMetadata() throws Exception {
        var env = ok(storeGet(COL, List.of(C1, C2, C3), List.of("metadatas"), true));
        assertThat(env).doesNotContainKey("documents");
        assertThat(env).containsKeys(ALWAYS.toArray(new String[0]));
        var metas = (List<Map<String, Object>>) env.get("metadatas");
        assertThat(metas).hasSize(3);
        assertThat(metas.get(((List<Object>) env.get("ids")).indexOf(C1)))
            .containsEntry("title", "T1").containsEntry("extra_key", "kept");
    }

    @Test
    void includeDocuments_returnsNoMetadatasKey_andTheTexts() throws Exception {
        var env = ok(storeGet(COL, List.of(C1, C2, C3), List.of("documents"), true));
        assertThat(env).doesNotContainKey("metadatas");
        assertThat(env).containsKeys(ALWAYS.toArray(new String[0]));
        assertThat((List<Object>) env.get("documents")).containsExactlyElementsOf(ORDERED_TEXT);
    }

    @Test
    void emptyInclude_isIdsOnlyPlusTheAlwaysKeys() throws Exception {
        var env = ok(storeGet(COL, List.of(C1, C2, C3), List.of(), true));
        assertThat(env).doesNotContainKeys("documents", "metadatas");
        assertThat(env).containsKeys(ALWAYS.toArray(new String[0]));
        assertThat((List<Object>) env.get("ids")).hasSize(3);
    }

    // (c) count / sensitives / spans / chashes do not move under projection -------------------------------

    @Test
    void alwaysKeys_areIdenticalUnderEveryProjection() throws Exception {
        var full = ok(storeGet(COL, List.of(C1, C2, C3), null, false));
        for (var include : List.of(List.of("metadatas"), List.of("documents"), List.<String>of())) {
            var proj = ok(storeGet(COL, List.of(C1, C2, C3), include, true));
            for (String k : ALWAYS) {
                assertThat(proj.get(k)).as("%s under include=%s", k, include).isEqualTo(full.get(k));
            }
        }
        // spans really are derived: chash form, line range, and the whole-document empty span
        assertThat((List<Object>) full.get("spans")).containsExactlyElementsOf(ORDERED_SPAN);
    }

    @Test
    void spans_surviveWhenMetadatasIsExcluded() throws Exception {
        var env = ok(storeGet(COL, List.of(C1, C2, C3), List.of("documents"), true));
        assertThat(env).doesNotContainKey("metadatas");
        assertThat((List<Object>) env.get("spans")).containsExactlyElementsOf(ORDERED_SPAN);
    }

    @Test
    void aTruncatedPage_hasTheSameCountChashesSpansAndSensitivesAsTheFullForm() throws Exception {
        Map<String, Object> limit2 = Map.of("limit", 2);
        var full = ok(storeGet(COL, List.of(C1, C2, C3), null, false, limit2));
        assertThat((List<Object>) full.get("ids")).containsExactlyElementsOf(ORDER.subList(0, 2));
        assertThat(((Number) full.get("count")).intValue()).as("count is over the whole matched set").isEqualTo(3);
        for (var include : List.of(List.of("metadatas"), List.of("documents"), List.<String>of())) {
            var proj = ok(storeGet(COL, List.of(C1, C2, C3), include, true, limit2));
            for (String k : List.of("ids", "count", "chashes", "spans", "sensitives")) {
                assertThat(proj.get(k)).as("%s of the truncated page under include=%s", k, include)
                    .isEqualTo(full.get(k));
            }
            assertThat((List<Object>) proj.get("spans")).containsExactlyElementsOf(ORDERED_SPAN.subList(0, 2));
        }
    }

    @Test
    void sensitives_arePresentAndTrueForASensitiveOwner_underProjection() throws Exception {
        var full = ok(storeGet(COL_S, List.of(CS), null, false));
        assertThat((List<Object>) full.get("sensitives")).containsExactly(true);
        var proj = ok(storeGet(COL_S, List.of(CS), List.of("metadatas"), true));
        assertThat((List<Object>) proj.get("sensitives")).containsExactly(true);
        assertThat(proj).doesNotContainKey("documents");
    }

    // sensitivity=exclude x include ------------------------------------------------------------------------

    @Test
    void excludeSensitive_dropsTheSensitiveRow_fromEveryListUnderProjection() throws Exception {
        Map<String, Object> exclude = Map.of("sensitivity", "exclude");
        var full = ok(storeGet(COL_X, List.of(X1, X2, XS), null, false, exclude));
        assertThat((List<Object>) full.get("ids")).containsExactlyElementsOf(X_LIVE);
        for (var include : List.of(List.of("metadatas"), List.of("documents"), List.<String>of())) {
            var proj = ok(storeGet(COL_X, List.of(X1, X2, XS), include, true, exclude));
            for (String k : List.of("ids", "count", "chashes", "spans", "sensitives")) {
                assertThat(proj.get(k)).as("%s under exclude and include=%s", k, include).isEqualTo(full.get(k));
            }
            assertThat((List<Object>) proj.get("sensitives")).containsExactly(false, false);
            assertThat(((Number) proj.get("count")).intValue()).isEqualTo(2);
            if (include.contains("metadatas")) {
                assertThat((List<Object>) proj.get("metadatas")).as("one metadata per surviving row").hasSize(2);
            }
            if (include.contains("documents")) {
                assertThat((List<Object>) proj.get("documents")).containsExactlyElementsOf(
                    X_LIVE.stream().map(h -> h.equals(X1) ? "x one" : "x two").toList());
            }
        }
    }

    @Test
    void withoutExclude_theSensitiveRowComesBackFlagged_underProjection() throws Exception {
        var proj = ok(storeGet(COL_X, List.of(X1, X2, XS), List.of("metadatas"), true));
        assertThat((List<Object>) proj.get("ids")).containsExactlyInAnyOrder(X1, X2, XS);
        var ids = (List<Object>) proj.get("ids");
        var flags = (List<Object>) proj.get("sensitives");
        for (int i = 0; i < ids.size(); i++) assertThat(flags.get(i)).isEqualTo(ids.get(i).equals(XS));
    }

    @Test
    void aDocumentFlaggedBetweenTheReadAndTheFlagLookup_isDroppedFromEveryParallelList_underProjection()
            throws Exception {
        int[] fired = {0};
        PgVectorRepository.setBeforeSensitiveLookupHookForTests(() -> {
            fired[0]++;
            flagDoc("x-doc-2", true);
        });
        try {
            var proj = ok(storeGet(COL_X, List.of(X1, X2), List.of("metadatas"), true,
                                   Map.of("sensitivity", "exclude")));
            assertThat(fired[0]).as("the seam fired: the test drove the window").isPositive();
            assertThat((List<Object>) proj.get("ids")).containsExactly(X1);
            assertThat(proj).doesNotContainKey("documents");
            for (String k : List.of("metadatas", "chashes", "spans", "sensitives")) {
                assertThat((List<Object>) proj.get(k)).as("%s lost the flagged row too", k).hasSize(1);
            }
            assertThat((List<Object>) proj.get("chashes")).containsExactly(X1);
            assertThat((List<Object>) proj.get("spans")).containsExactly("1-2");
            assertThat((List<Object>) proj.get("sensitives")).containsExactly(false);
            assertThat(((Number) proj.get("count")).intValue()).isEqualTo(1);
        } finally {
            PgVectorRepository.setBeforeSensitiveLookupHookForTests(null);
            flagDoc("x-doc-2", false);
        }
    }

    private void flagDoc(String doc, boolean sensitive) {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.SENSITIVE, sensitive)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.TUMBLER.eq(doc)).execute();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    // (d) bad include -> 400 ---------------------------------------------------------------------------------

    @Test
    void anUnknownIncludeValue_is400() throws Exception {
        var r = storeGet(COL, List.of(C1), List.of("metadatas", "bogus"), true);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(400);
        assertThat(r.body()).contains("include").contains("bogus");
    }

    @Test
    void aNonArrayInclude_is400() throws Exception {
        assertThat(storeGet(COL, List.of(C1), "metadatas", true).statusCode()).isEqualTo(400);
        assertThat(storeGet(COL, List.of(C1), List.of(1), true).statusCode()).isEqualTo(400);
    }

    @Test
    void includeNonLive_pathIsUntouched_evenWithAnIncludeArray() throws Exception {
        var body = new HashMap<String, Object>();
        body.put("collection", COL);
        body.put("ids", List.of(C1, C2));
        body.put("include", List.of("metadatas"));
        body.put("include_non_live", true);
        var env = ok(post(body));
        assertThat(env).containsKeys("ids", "metadatas").doesNotContainKey("documents");
        assertThat((List<Object>) env.get("ids")).containsExactlyInAnyOrder(C1, C2);
    }

    @Test
    void includeNonLive_ignoresABadInclude_soItIs200NotA400() throws Exception {
        // The include_non_live branch answers before include is parsed (VectorHandler), so a bad value there is
        // ignored, not refused; the same bad value without include_non_live is a 400 (anUnknownIncludeValue_is400).
        var body = new HashMap<String, Object>();
        body.put("collection", COL);
        body.put("ids", List.of(C1));
        body.put("include", List.of("bogus"));
        body.put("include_non_live", true);
        assertThat(post(body).statusCode()).isEqualTo(200);
    }
}
