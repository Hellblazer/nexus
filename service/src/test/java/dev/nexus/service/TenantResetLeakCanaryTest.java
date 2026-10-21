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
import dev.nexus.service.db.TenantKeyedStateGateTest;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.db.TupleRepository;
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
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.TreeMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-vr8wk (round-1 critic S4 of nexus-0bl77): the behavioural leak canary behind the tenant reset route.
 *
 * <p>{@code TenantKeyedStateGateTest} proves each holder of per-tenant state was acknowledged; it cannot prove the
 * acknowledgments are right (a holder wrongly filed as "not keyed" passes). This drives a real workload as tenant X
 * through a real {@link NexusService} (a token, a request that warms the token cache and the ghost-sweep marker, a
 * collection pair, a data-sweep failure, a data-token mint, a tuple write, a T2 memory write, a catalog collection
 * registration, a parked tuple read), resets X through the route, and walks the live object graph for X's name
 * ({@link TenantLeakWalker}): from the service, and from the static fields of every class the census catalogued.
 * After the reset X must be unreachable; tenant Y, given the same workload, must still be reachable in exactly the
 * structures X was, so the walk is neither blind nor a sledgehammer.
 *
 * <p><b>What this guards, and what it does not.</b> It guards the structures the workload FEEDS: a tenant-keyed
 * evictor whose structure the workload does not fill passes it trivially. {@link #theCataloguesEvictorsAreEachFedByTheWorkloadOrExemptedByName}
 * ties the two together: the evictors the census names must equal the fed set plus a named {@link #UNFED} set with a
 * reason each, so a new evictor fails the canary until it is fed or explicitly exempted. It is a regression guard
 * for those structures; it is NOT proof that reset is complete over the API surface the Python suite drives, and
 * it must not be cited as such for nexus-7zloy (the A/B failure-set equality remains the end-to-end check). The
 * walk is bounded as {@link TenantLeakWalker} says: a walk that hits a cap fails, and an opaque JDK or third-party
 * holder (its classes are printed in every failure here) is not seen.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TenantResetLeakCanaryTest {

    private static final String ROOT = "leak-canary-root-token";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String RESET = "/v1/_test/tenant-reset";
    /** Census classes the walk must read statics of: a floor, so a resolver that found nothing fails. */
    private static final int MIN_STATIC_OWNERS = 100;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
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
        Map<String, String> env = Map.of("NX_TEST_TENANT_RESET_ROUTE", "1");
        service = new NexusService(0, ROOT, ds, null, null, null, null, templates, null, env::get, () -> false);
        service.start();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── the canary ───────────────────────────────────────────────────────────────────────────────

    @Test
    void afterAResetTenantXIsUnreachableFromTheEngine_andTenantYIsStillThereInTheSameStructures() throws Exception {
        String x = "canary-x-" + shortId();
        String y = "canary-y-" + shortId();
        var parkedX = workload(x);
        var parkedY = workload(y);
        // The data-sweep failure streaks, through the sweep's own test seam: one pass, a failure for both tenants
        // (a pass that succeeds for a tenant clears its streak, so the two must fail in the same pass).
        service.dataTokenSweepOverride = t -> {
            if (t.equals(x) || t.equals(y)) throw new RuntimeException("simulated failure");
            return 0;
        };
        service.runScheduledSweep(OffsetDateTime.now(ZoneOffset.UTC));
        service.dataTokenSweepOverride = null;
        awaitParkedReads(2);   // both reads are inside their park, registered in the wait registry

        TenantLeakWalker.Result beforeX = walk(x);
        TenantLeakWalker.Result beforeY = walk(y);
        assertThat(beforeX.staticOwners()).as("the walk read the census's static holders").isGreaterThan(MIN_STATIC_OWNERS);
        assertThat(beforeX.hits()).as("positive control: the workload left X's name in the engine" + opaqueNote(beforeX))
            .isNotEmpty();
        assertThat(kinds(beforeX)).as("X reached through every structure the workload fed:\n" + beforeX.hits()
                + opaqueNote(beforeX)).containsAll(EXPECTED_KINDS);
        assertThat(kinds(beforeY)).as("Y, the same workload, is reachable in the same structures" + opaqueNote(beforeY))
            .isEqualTo(kinds(beforeX));

        var reset = post(RESET, ROOT, "{\"tenant\":\"" + x + "\"}");
        assertThat(reset.statusCode()).as(reset.body()).isEqualTo(200);
        assertThat(MAPPER.readTree(reset.body()).has("failed")).as(reset.body()).isFalse();
        assertThat(parkedX.get(5, TimeUnit.SECONDS).statusCode()).as("X's parked read was aborted").isEqualTo(409);

        TenantLeakWalker.Result afterX = walk(x);
        TenantLeakWalker.Result afterY = walk(y);
        assertThat(afterX.hits()).as("X's name is still reachable after the reset, in: " + kinds(afterX) + "\n"
            + afterX.hits() + opaqueNote(afterX)).isEmpty();
        assertThat(afterX.objects()).as("the walk after is a real walk, as big as the one before it")
            .isGreaterThan(beforeX.objects() / 2);
        assertThat(kinds(afterY)).as("Y was not touched by X's reset" + opaqueNote(afterY)).isEqualTo(kinds(beforeY));

        // Clean up Y (its parked read ends with its own reset) and prove that reset leaves nothing either.
        post(RESET, ROOT, "{\"tenant\":\"" + y + "\"}");
        assertThat(parkedY.get(5, TimeUnit.SECONDS).statusCode()).isEqualTo(409);
        TenantLeakWalker.Result afterReset = walk(y);
        assertThat(afterReset.hits()).as("and Y, once reset, leaves nothing too" + opaqueNote(afterReset)).isEmpty();
    }

    @Test
    void theCataloguesEvictorsAreEachFedByTheWorkloadOrExemptedByName() {
        Set<String> catalogued = TenantKeyedStateGateTest.evictorNames();
        assertThat(catalogued).as("non-vacuity: the catalogue names evictors").hasSizeGreaterThanOrEqualTo(8);
        assertThat(FED.keySet()).as("an evictor is either fed or exempted, never both").doesNotContainAnyElementsOf(UNFED.keySet());
        var accounted = new TreeSet<String>(FED.keySet());
        accounted.addAll(UNFED.keySet());
        assertThat(catalogued).as("every tenant-keyed evictor TenantKeyedStateGateTest catalogues is fed by this "
            + "canary's workload (add a workload step and a FED entry) or named in UNFED with its reason")
            .isEqualTo(accounted);
        assertThat(UNFED.values()).as("every exemption carries a reason").allSatisfy(r -> assertThat(r).isNotBlank());
    }

    /**
     * The evictors the census names that the workload feeds, each with the structure {@code kindOf} reports it as.
     * Keyed by the evictor name {@code TenantKeyedStateGateTest} files the holder under.
     */
    private static final Map<String, String> FED = new TreeMap<>(Map.of(
        "TokenCache", "TokenCache.map",
        "CollectionRegistry", "CollectionRegistry.KNOWN",
        "CatalogRepository.ghostSweepCheckedTenants", "ghostSweepCheckedTenants",
        "NexusService.dataSweepFailuresByTenant", "dataSweepFailuresByTenant",
        "TupleWaitRegistry", "TupleWaitRegistry.groups",
        "MintRateLimiter", "MintRateLimiter.credentials"));

    /** The evictors the census names that this canary does NOT feed, each with why. A new evictor lands in neither
     *  this map nor {@link #FED} and fails {@code theCataloguesEvictorsAreEachFed...} until it is placed in one. */
    private static final Map<String, String> UNFED = new TreeMap<>(Map.of(
        "ChunkReaper", "its timeout streaks and resume cursors are filled by a reaper pass over a T3 collection, "
            + "which needs a vector backend this engine does not have; TenantKeyedEvictorsTest covers its evictor",
        "OwnerlessLogLimiter", "its states are filled by an ownerless-chunk log line the reaper emits, so it needs "
            + "the same vector backend; TenantKeyedEvictorsTest covers its evictor"));

    /** The structures the workload feeds, named as {@code kindOf} names them: the positive control. */
    private static final List<String> EXPECTED_KINDS = List.copyOf(FED.values());

    /** Path tail to the structure it names. The same structure is reached by different routes (a field of the
     *  service, an evictor's captured reference), so a kind is read off the last field names, not the route. */
    private static final Map<String, String> KIND_BY_TAIL = Map.of(
        ".map[].tenantId", "TokenCache.map",
        "CollectionRegistry.KNOWN", "CollectionRegistry.KNOWN",
        ".ghostSweepCheckedTenants[]", "ghostSweepCheckedTenants",
        ".dataSweepFailuresByTenant", "dataSweepFailuresByTenant",
        ".groups", "TupleWaitRegistry.groups",
        ".credentials[].tenants", "MintRateLimiter.credentials");

    /** The structures a walk's hits sit in, as stable names, so before and after, X and Y compare. */
    private static TreeSet<String> kinds(TenantLeakWalker.Result r) {
        var out = new TreeSet<String>();
        for (TenantLeakWalker.Hit h : r.hits()) {
            out.add(kindOf(h.path()));
        }
        return out;
    }

    private static String kindOf(String path) {
        String normalised = path.replaceAll("\\[[^\\]]*\\]", "[]").replaceAll("\\{key\\}", "");
        for (var e : KIND_BY_TAIL.entrySet()) {
            if (normalised.contains(e.getKey())) {
                return e.getValue();
            }
        }
        return "other:" + normalised;
    }

    // ── the workload ─────────────────────────────────────────────────────────────────────────────

    /** Feeds every structure an engine without a vector backend has for {@code tenant}; returns its parked read. */
    private CompletableFuture<HttpResponse<String>> workload(String tenant) throws Exception {
        String raw = createTenant(tenant);
        assertThat(whoami(raw)).isEqualTo(200);   // token cache entry, ghost-sweep marker
        CollectionRegistry.markKnown(tenant, "c1", new CollectionRow("code", "1.1", "voyage-code-3", 1024, "live"));

        // A data-token mint debits the limiter's (credential, tenant) bucket.
        String mintCredential = new TokenStore(ds, Clock.systemUTC())
            .issueToken(TenantConstants.DEFAULT_TENANT, "canary-mint-" + shortId(), null, TokenStore.SCOPE_MINT)
            .rawToken();
        var mint = post("/v1/data-tokens/mint", mintCredential, "{\"tenant\":\"" + tenant + "\"}");
        assertThat(mint.statusCode()).as(mint.body()).isEqualTo(200);

        // A tuple write, a T2 write and a catalog collection registration: three more paths the pool suite drives.
        // None of them is expected to leave the tenant's name in a tenant-keyed structure; if one does, the walk
        // after the reset reports it as an "other:" structure, which is the point of feeding them.
        String to = "canary-" + shortId();
        var out = post("/v1/tuples/out", raw, "{\"subspace\":\"mailbox/" + to + "\",\"keys\":{\"to\":\"" + to
            + "\"},\"dims\":{\"from\":\"canary\"},\"body\":\"hello\",\"nonce\":\"n-" + shortId() + "\"}");
        assertThat(out.statusCode()).as(out.body()).isEqualTo(200);
        var mem = post("/v1/memory/put", raw,
            "{\"project\":\"canary\",\"title\":\"t-" + shortId() + "\",\"content\":\"hello " + tenant + "\"}");
        assertThat(mem.statusCode()).as(mem.body()).isEqualTo(200);
        var coll = post("/v1/catalog/collections/upsert", raw,
            "{\"name\":\"knowledge__canary-" + shortId() + "__voyage-context-3__v1\",\"content_type\":\"knowledge\",\"owner_id\":\"canary\",\"embedding_model\":\"voyage-context-3\"}");
        assertThat(coll.statusCode()).as(coll.body()).isEqualTo(200);

        String subspace = "ledger/canary-" + shortId();
        return CompletableFuture.supplyAsync(() -> {
            try {
                return post("/v1/tuples/rd", raw, "{\"subspace\":\"" + subspace + "\",\"timeout_s\":25}");
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
    }

    // ── the walk ─────────────────────────────────────────────────────────────────────────────────

    private TenantLeakWalker.Result walk(String tenant) throws Exception {
        var roots = new LinkedHashMap<String, Object>();
        roots.put("service", service);
        TenantLeakWalker.Result r = TenantLeakWalker.walk(tenant, roots, catalogedClasses());
        assertThat(r.staticSkipped()).as("every cataloged class's statics were readable").isEmpty();
        return r;
    }

    private List<Class<?>> catalogedClasses() throws Exception {
        Path classes = Path.of("target", "classes", "dev", "nexus");
        var names = new TreeSet<String>();
        try (Stream<Path> files = Files.walk(classes)) {
            for (Path file : files.filter(f -> f.toString().endsWith(".class")).toList()) {
                String base = file.getFileName().toString().replace(".class", "");
                for (String owner : TenantKeyedStateGateTest.catalogedOwnerNames()) {
                    if (base.equals(owner.replace('.', '$'))) {
                        names.add(classes.getParent().getParent().relativize(file).toString()
                            .replace('/', '.').replaceAll("\\.class$", ""));
                    }
                }
            }
        }
        var out = new ArrayList<Class<?>>();
        for (String n : names) {
            out.add(Class.forName(n, false, getClass().getClassLoader()));
        }
        return out;
    }

    /** The opaque-class histogram of a walk, for a failure message: what the walk reached but could not read. */
    private static String opaqueNote(TenantLeakWalker.Result r) {
        return "\nopaque classes the walk did not read into (class=instances): " + r.opaque();
    }

    /** Polls the tuple repository's park count until {@code n} reads are parked, or fails after a bounded wait. */
    private void awaitParkedReads(int n) throws Exception {
        var field = NexusService.class.getDeclaredField("tupleRepo");
        field.setAccessible(true);
        var repo = (TupleRepository) field.get(service);
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (repo.parkStats().globalInUse() < n) {
            if (System.nanoTime() >= deadline) {
                throw new AssertionError("expected " + n + " parked tuple reads within 10 s but the repository has "
                    + repo.parkStats().globalInUse() + " in a park");
            }
            Thread.sleep(10);
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────────────────────────

    private static String shortId() {
        return UUID.randomUUID().toString().substring(0, 8);
    }

    private HttpResponse<String> post(String path, String bearer, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + bearer).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body)).build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private int whoami(String bearer) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/_whoami")
            .header("Authorization", "Bearer " + bearer).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode();
    }

    private String createTenant(String tenant) throws Exception {
        var resp = post("/v1/tenants/create", ROOT, "{\"name\":\"" + tenant + "\"}");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(resp.body());
        return body.get("token").asText();
    }
}
