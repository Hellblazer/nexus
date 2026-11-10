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

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, the behavioural proof that {@code POST /v1/vectors/store-get} with {@code include} that leaves out
 * {@code documents} never reads {@code nexus.chunks.chunk_text} in the statement {@code PgVectorRepository#get}
 * actually runs. The service role here holds SELECT on every {@code nexus.chunks} column except {@code chunk_text}
 * (table-level SELECT revoked by jOOQ, then the column-level grant in
 * {@code db.changelog-test-chunks-text-column-select.xml}), so a statement that names the column fails with
 * Postgres's "permission denied" (a 500 from the handler) and one that does not carries on. A projection computed
 * by a helper the repository does not call, or a full-width select whose keys are stripped afterwards, fails
 * the metadata-only tests below; the full form failing for this role is the non-vacuity check on the revoke.
 *
 * <p>Its own class and its own database on the shared test cluster: the revoke would break every other test that reads chunk text as this role.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class StoreGetIncludeColumnPrivilegeTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN  = "f3yqd-colpriv-token-0123456789abcdef01";
    private static final String TENANT = "f3yqd-colpriv-tenant";
    private static final String COL    = "knowledge__f3yqdcol__voyage-context-3__v1";
    private static final String SVC_ROLE = "svc_storeget_cols_test";
    private static final String SVC_PASS = "svc_storeget_cols_pass";

    private static final String C1 = Chash.ofText("f3yqd-col-1").toHex();
    private static final String C2 = Chash.ofText("f3yqd-col-2").toHex();
    private static final String C3 = Chash.ofText("f3yqd-col-3").toHex();   // no span keys at all
    private static final String HASH_1 =
        "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899";
    /** store-get answers in chash order, not request order. */
    private static final List<String> ORDER = List.of(C1, C2, C3).stream().sorted().toList();
    private static final Map<String, String> SPAN = Map.of(C1, "chash:" + HASH_1, C2, "5-15", C3, "");
    private static final List<String> ORDERED_SPAN = ORDER.stream().map(SPAN::get).toList();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "f3yqd-colpriv");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        var embedder = new FakeEmbedder(1024);
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
            PgContainerHelper.insertChunks(ctx, TENANT, COL, List.of(C1, C2, C3),
                List.of("text one", "text two", "text three"),
                List.of(Map.of("title", "T1", "chunk_text_hash", HASH_1, "line_start", 10, "line_end", 20,
                               "extra_key", "kept"),
                        Map.of("title", "T2", "line_start", 5, "line_end", 15),
                        Map.of("title", "T3")),
                embedder);
            PgContainerHelper.ownChunks(ctx, TENANT, COL, C1, C2, C3);
        }
        // The seed is in. From here the service role cannot read chunk_text.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES)
                .revoke(DSL.privilege("SELECT")).on(CHUNKS).from(DSL.role(SVC_ROLE)).execute();
            PgContainerHelper.runSuperuserTestChangelog(su,
                "db/changelog-test/db.changelog-test-chunks-text-column-select.xml",
                "databasechangelog_test_chunks_text_column_select", Map.of("role", SVC_ROLE));
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private HttpResponse<String> storeGet(Object include, boolean withInclude) throws Exception {
        Map<String, Object> body = new HashMap<>();
        body.put("collection", COL);
        body.put("ids", List.of(C1, C2, C3));
        if (withInclude) body.put("include", include);
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/store-get")
            .header("Authorization", "Bearer " + TOKEN).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
            HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private static List<Object> list(Map<String, Object> env, String key) {
        return (List<Object>) env.get(key);
    }

    @Test
    void theFullForm_failsForThisRole_soTheRevokeIsReal() throws Exception {
        var r = storeGet(null, false);
        // The handler answers 500 with a generic body; the cause (permission denied on nexus.chunks) is in the log.
        // It is chunk_text alone: the metadata-only reads below run the same statement minus that column.
        assertThat(r.statusCode()).as("a statement that names chunk_text is refused: " + r.body()).isEqualTo(500);
    }

    @Test
    void includeDocuments_namesTheColumn_soItFailsToo() throws Exception {
        var r = storeGet(List.of("documents"), true);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(500);
    }

    @Test
    void includeMetadatas_runsAStatementThatNeverNamesChunkText() throws Exception {
        var r = storeGet(List.of("metadatas"), true);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        Map<String, Object> env = MAPPER.readValue(r.body(), MAP_TYPE);
        assertThat(env).doesNotContainKey("documents");
        assertThat(list(env, "ids")).containsExactlyElementsOf(ORDER);
        assertThat(list(env, "chashes")).containsExactlyElementsOf(ORDER);
        assertThat(list(env, "spans")).containsExactlyElementsOf(ORDERED_SPAN);
        assertThat(list(env, "sensitives")).containsExactly(false, false, false);
        assertThat(((Number) env.get("count")).intValue()).isEqualTo(3);
        @SuppressWarnings("unchecked")
        var metas = (List<Map<String, Object>>) (List<?>) list(env, "metadatas");
        assertThat(metas.get(ORDER.indexOf(C1))).containsEntry("title", "T1").containsEntry("extra_key", "kept");
        assertThat(metas.get(ORDER.indexOf(C3))).containsEntry("title", "T3");
    }

    @Test
    void includeNothing_runsAStatementThatNeverNamesChunkText_andSpansStillCome() throws Exception {
        var r = storeGet(List.of(), true);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        Map<String, Object> env = MAPPER.readValue(r.body(), MAP_TYPE);
        assertThat(env).doesNotContainKeys("documents", "metadatas");
        assertThat(list(env, "ids")).containsExactlyElementsOf(ORDER);
        assertThat(list(env, "spans")).containsExactlyElementsOf(ORDERED_SPAN);
    }
}
