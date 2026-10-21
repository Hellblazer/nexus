// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.CollectionRow;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.db.TenantKeyedStateGateTest;
import dev.nexus.service.tuples.TemplateRegistry;
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
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.SERVICE_TOKENS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-0bl77 (rtg20 phase A): {@code POST /v1/_test/tenant-reset}, end to end through real {@link NexusService}
 * instances. Covers the route's gates (absent without the flag, operator only, refused while {@code NX_SEARCH_PCI}
 * is on, advertised on {@code /v1/status}), its boot refusal on a non-loopback bind, and that a reset names every
 * evictor and clears tenant X without touching tenant Y. The per-structure semantics are
 * {@link TenantKeyedEvictorsTest}; the exhaustiveness of the evictor list is {@code TenantKeyedStateGateTest}.
 *
 * <p>Three engines share one Postgres: the flag off, the flag on, and the flag on with PCI on.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantResetRouteTest {

    private static final String ROOT = "reset-route-root-token";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String PATH = "/v1/_test/tenant-reset";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService off;
    NexusService on;
    NexusService onWithPci;
    final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), ROOT,
                TenantConstants.DEFAULT_TENANT, TokenStore.ROOT_TOKEN_LABEL, TokenStore.SCOPE_ROOT, null, null);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(12);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);

        TemplateRegistry templates = TemplateRegistry.loadAtBoot(NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        off = engine(templates, Map.of(), () -> false);
        on = engine(templates, Map.of("NX_TEST_TENANT_RESET_ROUTE", "1"), () -> false);
        onWithPci = engine(templates, Map.of("NX_TEST_TENANT_RESET_ROUTE", "1"), () -> true);
    }

    private NexusService engine(TemplateRegistry templates, Map<String, String> env,
                                java.util.function.BooleanSupplier pci) throws Exception {
        Function<String, String> lookup = env::get;
        var service = new NexusService(0, ROOT, ds, null, null, null, null, templates, null, lookup, pci);
        service.start();
        return service;
    }

    @AfterAll
    void stopAll() throws Exception {
        for (NexusService s : new NexusService[] {off, on, onWithPci}) {
            if (s != null) s.stop();
        }
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── gates ────────────────────────────────────────────────────────────────────────────────────

    @Test
    void withoutTheFlag_theRouteIsAbsent_andStatusSaysSo() throws Exception {
        assertThat(post(off, PATH, ROOT, "{\"tenant\":\"rr-any\"}").statusCode()).isEqualTo(404);
        assertThat(status(off).path("test_tenant_reset_route").asBoolean(true))
            .as("/v1/status reports the route as not registered").isFalse();
        assertThat(off.tenantKeyedState()).as("the evictors are built either way").isNotEmpty();
    }

    @Test
    void withTheFlag_statusAdvertisesTheRoute_andVersionDoesNot() throws Exception {
        JsonNode s = status(on);
        assertThat(s.has("test_tenant_reset_route")).isTrue();
        assertThat(s.get("test_tenant_reset_route").asBoolean()).isTrue();
        // The conexus image smoke fails on any /version key outside the public contract, so the flag lives on
        // /v1/status and /version never carries it, with the flag on or off.
        assertThat(version(on).has("test_tenant_reset_route")).as("/version, flag on").isFalse();
        assertThat(version(off).has("test_tenant_reset_route")).as("/version, flag off").isFalse();
    }

    @Test
    void anUnauthenticatedProbeSeparatesAbsentFromRegistered() throws Exception {
        // A probe with NO credential against the local engine (not what the cloud gate sends: through the edge the
        // gate carries the leg-B data bearer, because the edge answers an unauthenticated /v1 request with 403
        // whether or not the route exists, nexus-69be4). An engine without the flag has no context for the path
        // (the JDK server's own 404, not a JSON body); one with it answers 401 from the auth filter. The method
        // name says "unauthenticated" and that is all it pins.
        var absent = TestHttp.request(base(off) + PATH).POST(HttpRequest.BodyPublishers.ofString("{}")).build();
        assertThat(http.send(absent, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(404);
        var registered = TestHttp.request(base(on) + PATH).POST(HttpRequest.BodyPublishers.ofString("{}")).build();
        assertThat(http.send(registered, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(401);
    }

    @Test
    void anyValueOtherThanOneLeavesTheRouteAbsent() {
        for (String value : new String[] {"0", "true", "yes", "", " "}) {
            assertThat(NexusService.tenantResetRouteEnabled(Map.of("NX_TEST_TENANT_RESET_ROUTE", value)::get))
                .as("value '%s'", value).isFalse();
        }
        assertThat(NexusService.tenantResetRouteEnabled(Map.<String, String>of()::get)).isFalse();
        assertThat(NexusService.tenantResetRouteEnabled(Map.of("NX_TEST_TENANT_RESET_ROUTE", "1")::get)).isTrue();
    }

    @Test
    void aTenantTokenIsRefused403_andNothingIsEvicted() throws Exception {
        String tenant = "rr-nonop-" + shortId();
        String raw = createTenant(tenant);
        CollectionRegistry.markKnown(tenant, "c", row());
        try {
            var resp = post(on, PATH, raw, "{\"tenant\":\"" + tenant + "\"}");
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(403);
            assertThat(CollectionRegistry.isKnown(tenant, "c")).as("a refused call evicts nothing").isTrue();
        } finally {
            CollectionRegistry.evictTenant(tenant);
        }
    }

    /**
     * nexus-axzgk: the cloud gate's leg N sends exactly these two requests, {} with the leg-B data bearer and
     * {} with root. Both name no tenant, so the danger is a handler that falls back to the caller's own tenant;
     * each caller's tenant holds state here, and both must keep it.
     */
    @Test
    void theGatesEmptyBodyRequests_evictNothing() throws Exception {
        String tenant = "rr-gate-" + shortId();
        String raw = createTenant(tenant);
        String defaultCollection = "rr-gate-default-" + shortId();
        CollectionRegistry.markKnown(tenant, "c", row());
        CollectionRegistry.markKnown("default", defaultCollection, row());
        try {
            var data = post(on, PATH, raw, "{}");
            assertThat(data.statusCode()).as("data bearer + {}: %s", data.body()).isEqualTo(403);
            var root = post(on, PATH, ROOT, "{}");
            assertThat(root.statusCode()).as("root + {}: %s", root.body()).isEqualTo(400);
            assertThat(CollectionRegistry.isKnown(tenant, "c")).as("the data bearer's own tenant is untouched").isTrue();
            assertThat(CollectionRegistry.isKnown("default", defaultCollection)).as("root's own tenant is untouched").isTrue();
        } finally {
            CollectionRegistry.evictTenant(tenant);
            CollectionRegistry.evict("default", defaultCollection);
        }
    }

    @Test
    void noBearerIs401() throws Exception {
        var req = TestHttp.request(base(on) + PATH)
            .POST(HttpRequest.BodyPublishers.ofString("{\"tenant\":\"x\"}")).build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(401);
    }

    @Test
    void whileSearchPciIsOn_theRouteAnswers503_andEvictsNothing() throws Exception {
        String tenant = "rr-pci-" + shortId();
        CollectionRegistry.markKnown(tenant, "c", row());
        try {
            var resp = post(onWithPci, PATH, ROOT, "{\"tenant\":\"" + tenant + "\"}");
            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(503);
            assertThat(resp.body()).contains("NX_SEARCH_PCI");
            assertThat(CollectionRegistry.isKnown(tenant, "c")).isTrue();
        } finally {
            CollectionRegistry.evictTenant(tenant);
        }
    }

    @Test
    void badRequests_areRefusedBeforeAnyEviction() throws Exception {
        assertThat(get(on, PATH, ROOT).statusCode()).isEqualTo(405);
        assertThat(post(on, PATH, ROOT, "{}").statusCode()).as("no tenant").isEqualTo(400);
        assertThat(post(on, PATH, ROOT, "{\"tenant\":\"  \"}").statusCode()).as("blank tenant").isEqualTo(400);
        assertThat(post(on, PATH, ROOT, "not json").statusCode()).as("malformed body").isEqualTo(400);
        assertThat(post(on, PATH, ROOT, "{\"tenant\":\"default\"}").statusCode())
            .as("the default tenant is never reset").isEqualTo(400);
        assertThat(post(on, PATH, ROOT, "{\"tenant\":\"*\"}").statusCode()).isEqualTo(400);
    }

    // ── boot refusal ─────────────────────────────────────────────────────────────────────────────

    @Test
    void bootRefusesTheFlagOnANonLoopbackBind() {
        for (String bind : new String[] {"0.0.0.0", "10.1.2.3", "::", "example.internal"}) {
            Map<String, String> env = Map.of("NX_TEST_TENANT_RESET_ROUTE", "1", "NX_SERVICE_BIND", bind);
            assertThatThrownBy(() -> NexusService.verifyTenantResetRouteBind(env::get))
                .as("bind %s", bind).isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("NX_TEST_TENANT_RESET_ROUTE").hasMessageContaining(bind);
            assertThatThrownBy(() -> new NexusService(0, ROOT, ds, null, null, null, null, null, null, env::get, () -> false))
                .as("constructing the service with bind %s", bind).isInstanceOf(IllegalStateException.class);
        }
    }

    @Test
    void bootAllowsTheFlagOnLoopback_andAnyBindWithoutTheFlag() {
        for (String bind : new String[] {null, "", "127.0.0.1", "localhost", "::1"}) {
            Map<String, String> env = new java.util.HashMap<>();
            env.put("NX_TEST_TENANT_RESET_ROUTE", "1");
            if (bind != null) env.put("NX_SERVICE_BIND", bind);
            NexusService.verifyTenantResetRouteBind(env::get);
        }
        NexusService.verifyTenantResetRouteBind(Map.of("NX_SERVICE_BIND", "0.0.0.0")::get);
        NexusService.verifyTenantResetRouteBind(Map.of("NX_TEST_TENANT_RESET_ROUTE", "0", "NX_SERVICE_BIND", "0.0.0.0")::get);
    }

    // The Main-level refusal (exit 1 before the pool and the migration) is a real boot in a child JVM:
    // MainBootEnvValidationIntegrationTest.theTenantResetRouteOnANonLoopbackBind_*.

    // ── the reset ────────────────────────────────────────────────────────────────────────────────

    @Test
    void aResetNamesEveryEvictor_inRegistrationOrder_matchingTheGatesTable() throws Exception {
        String tenant = "rr-names-" + shortId();
        var resp = post(on, PATH, ROOT, "{\"tenant\":\"" + tenant + "\"}");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(resp.body());
        assertThat(body.get("tenant").asText()).isEqualTo(tenant);
        List<String> named = new ArrayList<>();
        body.get("evicted").fieldNames().forEachRemaining(named::add);
        assertThat(named).as("the response names exactly the evictors the gate test's table lists")
            .containsExactlyInAnyOrderElementsOf(TenantKeyedStateGateTest.evictorNames());
        assertThat(named).as("and exactly the ones the service registered")
            .containsExactlyElementsOf(on.tenantKeyedState().stream().map(s -> s.name()).toList());
        assertThat(new TreeSet<>(named)).as("no evictor is listed twice").hasSameSizeAs(named);
        assertThat(named.get(0)).as("the token cache goes first, so the old bearer stops working before "
            + "the tuple evictor's bounded wait").isEqualTo("TokenCache");
        body.get("evicted").forEach(count -> assertThat(count.isInt()).isTrue());
        assertThat(body.has("failed")).isFalse();
    }

    @Test
    void aResetClearsTenantXAndLeavesTenantY() throws Exception {
        String x = "rr-x-" + shortId();
        String y = "rr-y-" + shortId();
        String xRaw = createTenant(x);
        String yRaw = createTenant(y);
        try {
            // Warm what the engine caches per tenant: the token cache and the ghost-sweep marker (a request each).
            assertThat(whoami(on, xRaw)).isEqualTo(200);
            assertThat(whoami(on, yRaw)).isEqualTo(200);
            // The data-token sweep failure streaks, through the sweep's own test seam.
            on.dataTokenSweepOverride = t -> {
                if (t.equals(x) || t.equals(y)) throw new RuntimeException("simulated failure");
                return 0;
            };
            on.runScheduledSweep(OffsetDateTime.now(ZoneOffset.UTC));
            on.dataTokenSweepOverride = null;
            assertThat(on.dataSweepFailuresForTenant(x)).isEqualTo(1);
            assertThat(on.dataSweepFailuresForTenant(y)).isEqualTo(1);
            // The collection registry.
            CollectionRegistry.markKnown(x, "c1", row());
            CollectionRegistry.markKnown(x, "c2", row());
            CollectionRegistry.markKnown(y, "c1", row());
            // What the harness's DELETE does to the tenant's tokens: the rows are gone, the cache still answers.
            try (Connection su = pg.createConnection("")) {
                DSL.using(su, SQLDialect.POSTGRES).deleteFrom(SERVICE_TOKENS)
                    .where(SERVICE_TOKENS.TENANT_ID.eq(x)).execute();
            }
            assertThat(whoami(on, xRaw)).as("stale positive before the reset").isEqualTo(200);

            var resp = post(on, PATH, ROOT, "{\"tenant\":\"" + x + "\"}");

            assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
            JsonNode evicted = MAPPER.readTree(resp.body()).get("evicted");
            assertThat(evicted.get("TokenCache").asInt()).as("X's cached principal").isEqualTo(1);
            assertThat(evicted.get("CollectionRegistry").asInt()).as("X's two pairs").isEqualTo(2);
            assertThat(evicted.get("CatalogRepository.ghostSweepCheckedTenants").asInt()).isEqualTo(1);
            assertThat(evicted.get("NexusService.dataSweepFailuresByTenant").asInt()).isEqualTo(1);

            assertThat(whoami(on, xRaw)).as("X's deleted token is no longer answered from memory").isEqualTo(401);
            assertThat(whoami(on, yRaw)).as("Y still authenticates").isEqualTo(200);
            assertThat(CollectionRegistry.isKnown(x, "c1")).isFalse();
            assertThat(CollectionRegistry.isKnown(x, "c2")).isFalse();
            assertThat(CollectionRegistry.isKnown(y, "c1")).as("Y's pair is intact").isTrue();
            assertThat(on.dataSweepFailuresForTenant(x)).isZero();
            assertThat(on.dataSweepFailuresForTenant(y)).as("Y's streak is intact").isEqualTo(1);

            var again = post(on, PATH, ROOT, "{\"tenant\":\"" + x + "\"}");
            JsonNode second = MAPPER.readTree(again.body()).get("evicted");
            second.forEach(count -> assertThat(count.asInt()).as("a second reset has nothing left").isZero());
        } finally {
            on.dataTokenSweepOverride = null;
            CollectionRegistry.evictTenant(x);
            CollectionRegistry.evictTenant(y);
        }
    }

    @Test
    void aResetWakesAParkedTupleReadWithTenantReset409() throws Exception {
        String tenant = "rr-park-" + shortId();
        String raw = createTenant(tenant);
        String subspace = "ledger/park-" + shortId();
        CompletableFuture<HttpResponse<String>> parked = CompletableFuture.supplyAsync(() -> {
            try {
                return post(on, "/v1/tuples/rd", raw, "{\"subspace\":\"" + subspace + "\",\"timeout_s\":8}");
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
        Thread.sleep(700);   // let the read register and park

        long start = System.nanoTime();
        var reset = post(on, PATH, ROOT, "{\"tenant\":\"" + tenant + "\"}");

        assertThat(reset.statusCode()).as(reset.body()).isEqualTo(200);
        assertThat(MAPPER.readTree(reset.body()).get("evicted").get("TupleWaitRegistry").asInt())
            .as("the tenant's one wait group was dropped").isEqualTo(1);
        var read = parked.get(5, TimeUnit.SECONDS);
        assertThat(read.statusCode()).as(read.body()).isEqualTo(409);
        assertThat(MAPPER.readTree(read.body()).get("error").asText()).isEqualTo("TenantReset");
        assertThat(TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - start)).as("did not ride out the 8 s read")
            .isLessThan(5);
    }

    // ── helpers ──────────────────────────────────────────────────────────────────────────────────

    private static CollectionRow row() {
        return new CollectionRow("code", "1.1", "voyage-code-3", 1024, "live");
    }

    private static String shortId() {
        return UUID.randomUUID().toString().substring(0, 8);
    }

    private static String base(NexusService s) {
        return "http://127.0.0.1:" + s.getPort();
    }

    private HttpResponse<String> post(NexusService s, String path, String bearer, String body) throws Exception {
        var req = TestHttp.request(base(s) + path)
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body)).build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(NexusService s, String path, String bearer) throws Exception {
        var req = TestHttp.request(base(s) + path)
            .header("Authorization", "Bearer " + bearer).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode status(NexusService s) throws Exception {
        var req = TestHttp.request(base(s) + "/v1/status").GET().build();
        return MAPPER.readTree(http.send(req, HttpResponse.BodyHandlers.ofString()).body());
    }

    private JsonNode version(NexusService s) throws Exception {
        var req = TestHttp.request(base(s) + "/version").GET().build();
        return MAPPER.readTree(http.send(req, HttpResponse.BodyHandlers.ofString()).body());
    }

    private int whoami(NexusService s, String bearer) throws Exception {
        return get(s, "/v1/_whoami", bearer).statusCode();
    }

    /** Creates the tenant through the operator route of the flag-on engine and returns its first raw token. */
    private String createTenant(String tenant) throws Exception {
        var resp = post(on, "/v1/tenants/create", ROOT, "{\"name\":\"" + tenant + "\"}");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body()).get("token").asText();
    }
}
