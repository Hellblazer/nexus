// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.http.StatusHandler;
import dev.nexus.service.vectors.LeafBloatCatalog;
import dev.nexus.service.vectors.LeafBloatSweep;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Instant;
import java.util.List;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-i827c: {@code GET /v1/admin/chunks-leaves} through the real {@link NexusService} and its {@code AuthFilter}.
 *
 * <p>The security point is who may read leaf names. Pinned here, each against the live filter chain: no credential is a
 * 401, a credential the registry does not know is a 401, a live tenant-bound token is a 403 (the rows span every
 * tenant's leaves), a mint-scoped credential is refused (403, by the filter, before the handler), and only the root
 * token reads the list. A refused answer carries no leaf name. {@code GET /v1/status}, which is unauthenticated, stays
 * aggregates-only while this route is bound to the same leaves.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunksLeavesRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String ROOT = "tok-root-chunks-leaves-0123456789abcdef";
    private static final String TENANT_TOKEN = "tok-tenant-chunks-leaves-0123456789abcd";
    private static final String MINT_TOKEN = "tok-mint-chunks-leaves-0123456789abcdef";
    private static final String PINNED_LEAF = "chunks_pinned_leaf_hash01";
    private static final String UNSET_LEAF = "chunks_unset_leaf_hash02";
    private static final String CLEAN_LEAF = "chunks_clean_leaf_hash03";
    private static final String WAITING_LEAF = "chunks_waiting_leaf_hash04";
    private static final List<String> NAMES = List.of(PINNED_LEAF, UNSET_LEAF, CLEAN_LEAF, WAITING_LEAF);
    private static final Instant NOW = Instant.parse("2026-10-22T12:00:00Z");

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private NexusService service;
    private final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, ROOT, TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL,
                TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, "leaves-tenant", "tenant-token");
            PgContainerHelper.seedServiceToken(dsl, MINT_TOKEN, "leaves-tenant", "mint-token",
                TokenStore.SCOPE_MINT, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        service = new NexusService(0, ROOT, ds);
        service.start();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── fixtures ──────────────────────────────────────────────────────────────

    private static LeafBloatCatalog.Leaf leaf(String name, long live, long dead, long vacuums, boolean policySet,
                                              boolean autovacuumOn, long pciBytes) {
        List<LeafBloatCatalog.PciIndex> pci = pciBytes == 0 ? List.of()
            : List.of(new LeafBloatCatalog.PciIndex("pci_" + name, true, pciBytes));
        return new LeafBloatCatalog.Leaf(name, live, dead, 0, Instant.parse("2026-10-22T09:00:00Z"), vacuums, policySet,
            autovacuumOn, pci);
    }

    /** Four leaves: pinned, policy-unset, clean, and overdue-but-waiting (which is not listed). */
    private static LeafBloatSweep.Status view() {
        var pinned = new LeafBloatSweep.LeafStatus(leaf(PINNED_LEAF, 100_000, 30_000, 5, true, true, 4_096_000),
            NOW.minusSeconds(7200), 3, 7200);
        var unset = LeafBloatSweep.LeafStatus.clean(leaf(UNSET_LEAF, 5_000, 10, 1, false, true, 0));
        var clean = LeafBloatSweep.LeafStatus.clean(leaf(CLEAN_LEAF, 5_000, 10, 1, true, true, 8192));
        var waiting = new LeafBloatSweep.LeafStatus(leaf(WAITING_LEAF, 100_000, 30_000, 2, true, true, 0),
            NOW.minusSeconds(60), 2, 60);
        return new LeafBloatSweep.Status(true, NOW, null, 0, false, List.of(clean, pinned, unset, waiting));
    }

    private HttpResponse<String> get(String path, String bearer) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + service.getPort() + path);
        if (bearer != null) b.header("Authorization", "Bearer " + bearer);
        return http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    private void bind(Supplier<LeafBloatSweep.Status> view) {
        service.chunksLeaves(view, () -> 300L);
        service.chunksLeafBloat(() -> StatusHandler.ChunksLeafBloat.of(view.get(), 300L));
    }

    private static JsonNode row(JsonNode rows, String leaf) {
        for (JsonNode n : rows) {
            if (leaf.equals(n.path("leaf").asText())) return n;
        }
        throw new AssertionError("no row for " + leaf + " in " + rows);
    }

    private static void assertNamesNoLeaf(String body) {
        for (String name : NAMES) {
            assertThat(body).as("a refused answer must not name a leaf").doesNotContain(name);
        }
        assertThat(body).doesNotContain("pci_").doesNotContain("\"leaves\"");
    }

    // ── authorization ─────────────────────────────────────────────────────────

    @Test
    void noCredentialIsRefused_andTheBodyNamesNoLeaf() throws Exception {
        bind(ChunksLeavesRouteTest::view);
        HttpResponse<String> r = get("/v1/admin/chunks-leaves", null);
        assertThat(r.statusCode()).isEqualTo(401);
        assertNamesNoLeaf(r.body());
    }

    @Test
    void aCredentialTheRegistryDoesNotKnowIsRefused() throws Exception {
        bind(ChunksLeavesRouteTest::view);
        HttpResponse<String> r = get("/v1/admin/chunks-leaves", "tok-never-issued-0123456789abcdef0123456789");
        assertThat(r.statusCode()).isEqualTo(401);
        assertNamesNoLeaf(r.body());
    }

    @Test
    void aTenantBoundTokenIsForbidden_becauseTheRowsSpanEveryTenant() throws Exception {
        bind(ChunksLeavesRouteTest::view);
        HttpResponse<String> r = get("/v1/admin/chunks-leaves", TENANT_TOKEN);
        assertThat(r.statusCode()).isEqualTo(403);
        assertNamesNoLeaf(r.body());
    }

    @Test
    void aMintScopedCredentialIsForbidden() throws Exception {
        bind(ChunksLeavesRouteTest::view);
        HttpResponse<String> r = get("/v1/admin/chunks-leaves", MINT_TOKEN);
        assertThat(r.statusCode()).isEqualTo(403);
        assertNamesNoLeaf(r.body());
    }

    @Test
    void onlyAGetIsAnswered() throws Exception {
        bind(ChunksLeavesRouteTest::view);
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/admin/chunks-leaves")
            .header("Authorization", "Bearer " + ROOT).POST(HttpRequest.BodyPublishers.noBody()).build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(405);
    }

    // ── the listing ───────────────────────────────────────────────────────────

    @Test
    void theRootTokenReadsTheLeavesThatNeedAttention_andOnlyThose() throws Exception {
        bind(ChunksLeavesRouteTest::view);
        HttpResponse<String> r = get("/v1/admin/chunks-leaves", ROOT);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(r.body());
        assertThat(body.path("leaves_total").asInt()).isEqualTo(4);
        assertThat(body.path("needing_attention").asInt()).isEqualTo(2);
        assertThat(body.path("sweep_seconds").asLong()).isEqualTo(300L);
        assertThat(body.path("ever_read").asBoolean()).isTrue();
        assertThat(body.path("expired").asBoolean()).isFalse();
        assertThat(body.path("last_read_at").asText()).isEqualTo("2026-10-22T12:00:00Z");

        JsonNode rows = body.path("leaves");
        assertThat(rows).hasSize(2);
        JsonNode pinned = row(rows, PINNED_LEAF);
        assertThat(pinned.path("overdue_state").asText()).isEqualTo("pinned");
        assertThat(pinned.path("problems")).extracting(JsonNode::asText).containsExactly("pinned");
        assertThat(pinned.path("rows").asLong()).isEqualTo(130_000L);
        assertThat(pinned.path("dead_rows").asLong()).isEqualTo(30_000L);
        assertThat(pinned.path("last_autovacuum").asText()).isEqualTo("2026-10-22T09:00:00Z");
        assertThat(pinned.path("pci_index_count").asInt()).isEqualTo(1);
        assertThat(pinned.path("pci_index_bytes").asLong()).isEqualTo(4_096_000L);
        assertThat(pinned.path("vacuums_since_overdue").asLong()).isEqualTo(2L);

        JsonNode unset = row(rows, UNSET_LEAF);
        assertThat(unset.path("overdue_state").asText()).isEqualTo("none");
        assertThat(unset.path("policy_set").asBoolean()).isFalse();
        assertThat(unset.path("problems")).extracting(JsonNode::asText).containsExactly("policy_unset");
        assertThat(unset.path("pci_index_bytes").asLong()).isZero();

        assertThat(r.body()).as("a clean leaf and a merely waiting one are not listed")
            .doesNotContain(CLEAN_LEAF).doesNotContain(WAITING_LEAF);
    }

    @Test
    void beforeTheSweepIsBoundTheRouteAnswers503() throws Exception {
        service.chunksLeaves(() -> null, () -> null);
        HttpResponse<String> r = get("/v1/admin/chunks-leaves", ROOT);
        assertThat(r.statusCode()).isEqualTo(503);
        assertNamesNoLeaf(r.body());
    }

    // ── status stays aggregates-only ──────────────────────────────────────────

    @Test
    void theUnauthenticatedStatusRouteStillNamesNoLeaf_whileTheListingRouteIsBound() throws Exception {
        bind(ChunksLeavesRouteTest::view);
        HttpResponse<String> r = get("/v1/status", null);
        assertThat(r.statusCode()).isEqualTo(200);
        JsonNode bloat = MAPPER.readTree(r.body()).path("chunks_leaf_bloat");
        assertThat(bloat.isObject()).isTrue();
        assertThat(bloat.path("leaves_total").asInt()).isEqualTo(4);
        assertThat(bloat.has("leaves")).isFalse();
        assertNamesNoLeaf(r.body());
        assertThat(r.body()).doesNotContain("pci_index_bytes");
    }
}
