// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-239 Phase 2 Step 2 (bead nexus-mdbds.2.12): {@code GET /v1/tuples/registry} serves a
 * template's {@code doc} block, and only when the caller asks with {@code ?doc=1} (S4 Q1 = B,
 * Sam 2026-11-01). The default response stays the size it was: the hot callers (tuple_subscribe,
 * nx doctor, the mailbox-arm probe) never read doc.
 *
 * <p>The documented template is written into a temp dir and loaded through
 * {@code TemplateRegistry.loadAtBoot(dir, ...)}, the NX_TUPLE_TEMPLATE_DIR mechanism, so the
 * test does not depend on which bundled templates carry a doc block.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleRegistryDocRouteTest {

    private static final String TOKEN = "tuple-registry-doc-token-xyz123";
    private static final String SVC_ROLE = "svc_tuple_registry_doc";
    private static final String SVC_PASS = "svc_tuple_registry_doc_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {
    };

    private static final String DOCUMENTED = "docprobe/<x>";
    private static final String PLAIN = "plainprobe/<x>";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    NexusService svc;
    TemplateRegistry registry;
    HttpClient http;
    ObjectMapper mapper;

    @BeforeAll
    void startAll(@TempDir Path templateDir) throws Exception {
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
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        // Every doc field: a value map under fields, a structured body, and an example whose
        // Long key is read back as a string.
        Files.writeString(templateDir.resolve("docprobe.yaml"), """
                name: docprobe/<x>
                keys:
                  - k
                dimensions:
                  d:
                    type: string
                    values: [one, two]
                id_from: keys+nonce
                take:
                  enabled: false
                retention_seconds: 100
                doc:
                  summary: A probe template.
                  writers:
                    - the probe writer
                  readers:
                    - Unverified: nobody reads it
                  fields:
                    k: the key
                    d:
                      one: first
                      two: second
                  body:
                    text: the message text
                    n: a count
                  example:
                    subspace: docprobe/thing
                    nonce: n1
                    keys:
                      k: 3889972
                    dims:
                      d: one
                    body: hello
                  see_also:
                    - RDR-239
                """, StandardCharsets.UTF_8);
        Files.writeString(templateDir.resolve("plainprobe.yaml"), """
                name: plainprobe/<x>
                keys:
                  - k
                id_from: keys
                take:
                  enabled: false
                retention_seconds: 100
                """, StandardCharsets.UTF_8);

        registry = TemplateRegistry.loadAtBoot(templateDir.toString(), null,
                NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        svc = new NexusService(0, TOKEN, svcDs, null, null, null, null, registry);
        svc.start();
        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svc != null) {
            svc.stop();
        }
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void docRoundTripsWithEveryFieldIntactOnDocEquals1() throws Exception {
        Map<String, Object> t = template(get("/v1/tuples/registry?doc=1"), DOCUMENTED);

        Map<String, Object> expected = new LinkedHashMap<>();
        expected.put("summary", "A probe template.");
        expected.put("writers", List.of("the probe writer"));
        expected.put("readers", List.of("Unverified: nobody reads it"));
        Map<String, Object> fields = new LinkedHashMap<>();
        fields.put("k", "the key");
        fields.put("d", Map.of("one", "first", "two", "second"));
        expected.put("fields", fields);
        expected.put("body", Map.of("text", "the message text", "n", "a count"));
        Map<String, Object> example = new LinkedHashMap<>();
        example.put("subspace", "docprobe/thing");
        example.put("nonce", "n1");
        example.put("keys", Map.of("k", "3889972"));
        example.put("dims", Map.of("d", "one"));
        example.put("body", "hello");
        expected.put("example", example);
        expected.put("see_also", List.of("RDR-239"));

        assertThat(t.get("doc")).isEqualTo(expected);
        // the fixed field order, not just map equality
        @SuppressWarnings("unchecked")
        Map<String, Object> doc = (Map<String, Object>) t.get("doc");
        assertThat(List.copyOf(doc.keySet()))
                .containsExactly("summary", "writers", "readers", "fields", "body", "example", "see_also");
    }

    @Test
    void defaultResponseCarriesNoDocKeyOnAnyTemplate() throws Exception {
        Map<String, Object> body = get("/v1/tuples/registry");
        List<?> templates = (List<?>) body.get("templates");
        assertThat(templates).isNotEmpty();
        for (Object o : templates) {
            assertThat(((Map<?, ?>) o).containsKey("doc")).as("template %s", ((Map<?, ?>) o).get("name")).isFalse();
        }
        // the documented template is present, just without its doc
        assertThat(template(get("/v1/tuples/registry"), DOCUMENTED)).doesNotContainKey("doc");
    }

    @Test
    void onlyDocEquals1AsksForDoc() throws Exception {
        for (String q : List.of("doc=0", "doc=", "doc=true", "other=1")) {
            assertThat(template(get("/v1/tuples/registry?" + q), DOCUMENTED))
                    .as("?" + q).doesNotContainKey("doc");
        }
    }

    @Test
    void aTemplateWithoutDocHasNoDocKeyEvenWithDocEquals1() throws Exception {
        assertThat(template(get("/v1/tuples/registry?doc=1"), PLAIN)).doesNotContainKey("doc");
    }

    @Test
    void digestMatchesTheRegistryWithAndWithoutDoc() throws Exception {
        assertThat(get("/v1/tuples/registry").get("digest")).isEqualTo(registry.digest());
        assertThat(get("/v1/tuples/registry?doc=1").get("digest")).isEqualTo(registry.digest());
    }

    /** L6 (nexus-mdbds.2.14): the ledger says doc is appended LAST on the template object. */
    @Test
    void docIsTheLastKeyOfATemplateObjectAndTheOtherKeysKeepTheirOrder() throws Exception {
        Map<String, Object> t = template(get("/v1/tuples/registry?doc=1"), DOCUMENTED);
        assertThat(List.copyOf(t.keySet())).containsExactly("name", "keys", "dimensions", "id_from", "id_dims",
                "take", "retention_seconds", "lock", "doc");
        assertThat(List.copyOf(template(get("/v1/tuples/registry"), DOCUMENTED).keySet()))
                .containsExactly("name", "keys", "dimensions", "id_from", "id_dims", "take", "retention_seconds",
                        "lock");
    }

    /** The default body is the ?doc=1 body with each template's doc removed: same keys, same
     *  order, same values, digest and sources included. */
    @Test
    void theDefaultBodyEqualsTheDocBodyWithEachDocKeyRemoved() throws Exception {
        Map<String, Object> withDoc = get("/v1/tuples/registry?doc=1");
        Map<String, Object> plain = get("/v1/tuples/registry");
        List<?> docTemplates = (List<?>) withDoc.get("templates");
        assertThat(docTemplates.stream().filter(o -> ((Map<?, ?>) o).containsKey("doc")).count())
                .as("non-vacuity: at least the documented probe carries a doc").isGreaterThanOrEqualTo(1);
        for (Object o : docTemplates) {
            ((Map<?, ?>) o).remove("doc");
        }
        assertThat(withDoc).isEqualTo(plain);
        assertThat(List.copyOf(withDoc.keySet())).isEqualTo(List.copyOf(plain.keySet()));
        List<?> plainTemplates = (List<?>) plain.get("templates");
        assertThat(docTemplates).hasSameSizeAs(plainTemplates);
        for (int i = 0; i < plainTemplates.size(); i++) {
            assertThat(List.copyOf(((Map<?, ?>) docTemplates.get(i)).keySet()))
                    .isEqualTo(List.copyOf(((Map<?, ?>) plainTemplates.get(i)).keySet()));
        }
    }

    /**
     * The nearest malformed escape that reaches the handler. {@code %zz} and a lone {@code %}
     * never do: the JDK HttpServer answers its own 400 while parsing the request line (measured
     * 2026-11-01), so the route cannot be asked about them. {@code %ff} is not valid UTF-8 and
     * decodes to U+FFFD: the request is a 200 and the doc flag still reads independently of it.
     */
    @Test
    void anUndecodableButWellFormedEscapeInAnotherParameterDoesNotAffectTheDocFlag() throws Exception {
        assertThat(template(get("/v1/tuples/registry?x=%ff"), DOCUMENTED)).doesNotContainKey("doc");
        assertThat(template(get("/v1/tuples/registry?doc=1&x=%ff"), DOCUMENTED)).containsKey("doc");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private static Map<String, Object> template(Map<String, Object> body, String name) {
        return ((List<Map<String, Object>>) body.get("templates")).stream()
                .filter(t -> name.equals(t.get("name")))
                .findFirst()
                .orElseThrow(() -> new AssertionError("template " + name + " not in the response"));
    }

    private Map<String, Object> get(String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + svc.getPort() + path)
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .GET()
                .build();
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as(path).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }
}
