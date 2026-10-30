// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-jypmj.1.13 — HTTP coverage for the optional {@code scope_filter} of {@code POST /v1/catalog/traverse}
 * ({@link dev.nexus.service.http.CatalogHandler#handleTraverse}). The predicate itself is pinned against the
 * repository in {@code CatalogRepositoryTest}; this class pins the WIRE: the request key is read, the
 * {@code scope_filter} marker comes back only when a filter was sent, and a malformed filter is a 400 rather
 * than a silently unfiltered (and therefore uncontained) walk.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerTraverseScopeFilterTest {

    private static final String TOKEN    = "traverse-scope-test-token-xyz456";
    private static final String SVC_ROLE = "svc_traverse_scope_test";
    private static final String SVC_PASS = "svc_traverse_scope_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;
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
        // Burn the per-tenant once-per-process ghost sweep on a request that registers nothing (the same
        // warm-up CatalogHandlerManifestEnvelopeTest runs, for the same reason).
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
        fixture();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String registerDoc(String ownerPrefix, String title, String collection, String contentType) throws Exception {
        var resp = post("/v1/catalog/doc/register", mapper.writeValueAsString(Map.of(
            "owner_prefix", ownerPrefix, "title", title, "content_type", contentType,
            "source_uri", "file:///" + ownerPrefix + "/" + title + ".md", "physical_collection", collection)));
        assertThat(resp.statusCode()).as("doc registration: " + resp.body()).isEqualTo(200);
        return (String) mapper.readValue(resp.body(), MAP_T).get("tumbler");
    }

    private void link(String from, String to) throws Exception {
        var resp = post("/v1/catalog/link", mapper.writeValueAsString(Map.of(
            "from_tumbler", from, "to_tumbler", to, "link_type", "cites", "created_by", "test")));
        assertThat(resp.statusCode()).as("link: " + resp.body()).isEqualTo(200);
    }

    private void link(String from, String to, String linkType) throws Exception {
        var resp = post("/v1/catalog/link", mapper.writeValueAsString(Map.of(
            "from_tumbler", from, "to_tumbler", to, "link_type", linkType, "created_by", "test")));
        assertThat(resp.statusCode()).as("link: " + resp.body()).isEqualTo(200);
    }

    private Map<String, Object> traverseRaw(Map<String, Object> body) throws Exception {
        var resp = post("/v1/catalog/traverse", mapper.writeValueAsString(body));
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    private Map<String, Object> traverse(Map<String, Object> extra) throws Exception {
        var body = new java.util.LinkedHashMap<String, Object>(Map.of(
            "seeds", List.of(seed), "link_types", List.of("cites"), "direction", "out", "depth", 1));
        body.putAll(extra);
        var resp = post("/v1/catalog/traverse", mapper.writeValueAsString(body));
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return mapper.readValue(resp.body(), MAP_T);
    }

    private String seed;
    private String inScope;
    private String outOfScope;

    private void fixture() throws Exception {
        seed = registerDoc("7.9", "seed", "rdr__tsf-owner__voyage-context-3__v1", "rdr");
        inScope = registerDoc("7.9", "in-scope", "rdr__tsf-owner__voyage-context-3__v1", "rdr");
        outOfScope = registerDoc("7.9", "out-of-scope", "code__tsf-owner__voyage-code-3__v1", "code");
        link(seed, inScope);
        link(seed, outOfScope);
    }

    @SuppressWarnings("unchecked")
    private static List<String> tumblers(Map<String, Object> body) {
        return ((List<Map<String, Object>>) body.get("nodes")).stream().map(n -> (String) n.get("tumbler")).toList();
    }

    @Test
    @SuppressWarnings("unchecked")
    void aScopeFilterRemovesTheOutOfScopeNodesAndReturnsTheMarker() throws Exception {
        var body = traverse(Map.of("scope_filter",
            Map.of("collections", List.of("rdr__tsf-owner__voyage-context-3__v1"))));

        assertThat(tumblers(body)).containsExactlyInAnyOrder(seed, inScope);
        assertThat((Map<String, Object>) body.get("scope_filter"))
            .containsEntry("applied", true).containsEntry("kind", "collections")
            .containsEntry("collection_count", 1).containsEntry("dropped_out_of_scope", 1);
    }

    @Test
    @SuppressWarnings("unchecked")
    void aSubtreeScopeFilterJudgesTheTumbler() throws Exception {
        var body = traverse(Map.of("scope_filter", Map.of("subtree", inScope)));

        assertThat(tumblers(body)).as("the seed is exempt; only the in-scope document's own tumbler is admitted")
            .containsExactlyInAnyOrder(seed, inScope);
        assertThat((Map<String, Object>) body.get("scope_filter"))
            .containsEntry("kind", "subtree").containsEntry("subtree", inScope).containsEntry("dropped_out_of_scope", 1);
    }

    @Test
    void noScopeFilterMeansNoMarkerAndTheWholeWalk() throws Exception {
        var body = traverse(Map.of());

        assertThat(tumblers(body)).containsExactlyInAnyOrder(seed, inScope, outOfScope);
        assertThat(body).as("a request without the predicate is answered as before").doesNotContainKey("scope_filter");
    }

    // ── nexus-jypmj.18: the depth and role of every returned node, even one reached only through a removed relay ──

    private static final String RDR_COLL  = "rdr__tsf-walk__voyage-context-3__v1";
    private static final String CODE_COLL = "code__tsf-walk__voyage-code-3__v1";

    @SuppressWarnings("unchecked")
    private static Map<String, Map<String, Object>> nodeWalk(Map<String, Object> body) {
        return (Map<String, Map<String, Object>>) body.get("node_walk");
    }

    @Test
    @SuppressWarnings("unchecked")
    void aNodeReachedOnlyThroughARemovedRelayStillCarriesItsDepthAndRole() throws Exception {
        // S (rdr, seed) -implements-> R (code, out of scope) -implements-> B (rdr, in scope): B is at depth 2
        // and the scope filter removes R, so no edge reaches B in the reply.
        String s = registerDoc("7.8", "relay-seed", RDR_COLL, "rdr");
        String r = registerDoc("7.8", "relay-code", CODE_COLL, "code");
        String b = registerDoc("7.8", "relay-reached", RDR_COLL, "rdr");
        link(s, r, "implements");
        link(r, b, "cites");

        var body = traverseRaw(Map.of("seeds", List.of(s), "direction", "out", "depth", 3,
            "link_types", List.of("implements", "cites"),
            "scope_filter", Map.of("collections", List.of(RDR_COLL))));

        assertThat(tumblers(body)).containsExactlyInAnyOrder(s, b);
        assertThat((List<?>) body.get("edges")).as("R took its edges with it").isEmpty();
        assertThat(nodeWalk(body)).containsOnlyKeys(s, b);
        assertThat(nodeWalk(body).get(s)).as("a seed is depth 0 with no role").containsEntry("depth", 0).doesNotContainKey("role");
        assertThat(nodeWalk(body).get(b)).as("B's depth and the role of the link that reached it, R -> B")
            .containsEntry("depth", 2).containsEntry("role", "citation");
    }

    @Test
    void theRoleIsTheBestOfTheLinksFromThePreviousLevelAndFollowsTheDirection() throws Exception {
        // X is reached at depth 1 by a 'relates' link from seed S1 and a 'cites' link from seed S2: citation wins.
        // Q points AT seed S1 with 'implements': under direction "both" it is depth 1, implementation.
        // W is reached by an unknown link type only: related.
        String s1 = registerDoc("7.8", "best-seed-1", RDR_COLL, "rdr");
        String s2 = registerDoc("7.8", "best-seed-2", RDR_COLL, "rdr");
        String x = registerDoc("7.8", "best-x", RDR_COLL, "rdr");
        String q = registerDoc("7.8", "best-q", RDR_COLL, "rdr");
        String w = registerDoc("7.8", "best-w", RDR_COLL, "rdr");
        link(s1, x, "relates");
        link(s2, x, "cites");
        link(q, s1, "implements");
        link(s2, w, "relates");

        var body = traverseRaw(Map.of("seeds", List.of(s1, s2), "direction", "both", "depth", 1,
            "link_types", List.of("relates", "cites", "implements"),
            "scope_filter", Map.of("collections", List.of(RDR_COLL))));

        var walk = nodeWalk(body);
        assertThat(walk.get(x)).containsEntry("depth", 1).containsEntry("role", "citation");
        assertThat(walk.get(q)).containsEntry("depth", 1).containsEntry("role", "implementation");
        assertThat(walk.get(w)).containsEntry("depth", 1).containsEntry("role", "related");

        var outOnly = traverseRaw(Map.of("seeds", List.of(s1, s2), "direction", "out", "depth", 1,
            "link_types", List.of("relates", "cites", "implements"),
            "scope_filter", Map.of("collections", List.of(RDR_COLL))));
        assertThat(nodeWalk(outOnly)).as("direction out does not walk the link that points at a seed").doesNotContainKey(q);
    }

    @Test
    void theLinkRoleTableMatchesTheClientsAndANullOrUnknownTypeIsRelated() {
        for (String t : List.of("cites", "quotes", "supersedes")) {
            assertThat(dev.nexus.service.db.CatalogRepository.linkRole(t)).as(t).isEqualTo("citation");
        }
        for (String t : List.of("implements", "implements-heuristic")) {
            assertThat(dev.nexus.service.db.CatalogRepository.linkRole(t)).as(t).isEqualTo("implementation");
        }
        assertThat(dev.nexus.service.db.CatalogRepository.linkRole("relates")).isEqualTo("related");
        assertThat(dev.nexus.service.db.CatalogRepository.linkRole("anything-else")).isEqualTo("related");
        assertThat(dev.nexus.service.db.CatalogRepository.linkRole(null)).isEqualTo("related");
    }

    @Test
    void withoutAScopeFilterTheReplyCarriesNoNodeWalk() throws Exception {
        assertThat(traverse(Map.of())).doesNotContainKey("node_walk");
    }

    @Test
    void aMalformedScopeFilterIsA400NotAnUnfilteredWalk() throws Exception {
        for (Object bad : List.of(
                "rdr",
                Map.of(),
                Map.of("collections", List.of("a"), "subtree", "1.2"),
                Map.of("collections", "rdr"),
                Map.of("collections", List.of(" ")),
                Map.of("subtree", "rdr.*"),
                Map.of("kind", "corpus_prefix", "value", "rdr"))) {
            var resp = post("/v1/catalog/traverse", mapper.writeValueAsString(Map.of(
                "seeds", List.of(seed), "scope_filter", bad)));
            assertThat(resp.statusCode()).as("scope_filter=" + bad).isEqualTo(400);
            assertThat(resp.body()).contains("scope_filter");
        }
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
