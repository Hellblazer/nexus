// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.NexusService;
import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.http.StatusHandler;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), review N2, linda's ruling: {@code GET /v1/status} is unauthenticated, so its
 * {@code rdr229_completion} body must carry no number that describes tenants (no registry row count, no tombstone count, no
 * orphan or unstamped count). Those counts are served by {@code GET /v1/catalog/rdr229_completion_counts}, which is
 * authenticated and scoped to the CALLER'S tenant: a tenant reads its own live collections, dangling tombstones by content
 * type, self-id rows, and orphan and unstamped counts per table; no caller ever sees another tenant's count or a total.
 *
 * <p>Two tenants with distinct numbers of everything, so a read that returned a total, or the other tenant's counts, differs
 * from the right answer.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229CompletionTenantCountsRouteTest {

    private static final String TOKEN_A = "rdr229-counts-token-a";
    private static final String TOKEN_B = "rdr229-counts-token-b";
    private static final String SVC_ROLE = "svc_rdr229_counts_test";
    private static final String SVC_PASS = "svc_rdr229_counts_test_pass";
    private static final String TENANT_A = TenantConstants.DEFAULT_TENANT;
    private static final String TENANT_B = "tenant-b-rdr229-counts";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> NAME = DSL.field(DSL.name("name"), String.class);
    private static final Field<String> SUPERSEDED_BY = DSL.field(DSL.name("superseded_by"), String.class);
    private static final Field<Long> SUPERSEDED_BY_ID = DSL.field(DSL.name("superseded_by_id"), Long.class);
    private static final Field<Long> COLLECTION_ID = DSL.field(DSL.name("collection_id"), Long.class);

    /** The keys of the field that are bookkeeping about the engine's own state, not about any tenant's data. */
    private static final Set<String> STATUS_KEYS = Set.of("gate1", "gate2", "gate3", "gates_holding", "gates_total", "summary", "gate3_why",
        "labels_total", "labels_incomplete", "checks_missing", "validate_waits",
        // RDR-229 P3.4: three install-wide booleans (the latch row exists; an id-predicate index is INVALID; the latch recorded PCI off), no tenant count or name
        "gate3_latched", "id_index_invalid", "gate3_latch_pci_off",
        // RDR-229 P3.7: what a collection rename waits for, one word from a closed set (none, restart, ...): install-wide, no tenant count or name
        "rename_missing");
    private static final Set<String> NUMERIC_STATUS_KEYS = Set.of("gates_holding", "gates_total", "labels_total");
    /** A number in front of what it counts: the shape of every tenant count the old text carried. */
    private static final Pattern TENANT_COUNT_TEXT = Pattern.compile("\\d+\\s+(orphan|unstamped|registry|collection|tombstone|live|row)", Pattern.CASE_INSENSITIVE);

    private PostgreSQLContainer<?> pg;
    private Connection su;
    private DSLContext ctx;
    private NexusService service;
    private HttpClient http;
    private com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        ctx = DSL.using(su, SQLDialect.POSTGRES);
        PgContainerHelper.applyProductSchema(su);
        PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        PgContainerHelper.seedServiceToken(ctx, TOKEN_A, TENANT_A, "test-bound-a");
        PgContainerHelper.seedServiceToken(ctx, TOKEN_B, TENANT_B, "test-bound-b");
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, TOKEN_A, svcDs);
        service.start();
        http = TestHttp.client();
        // Burn the per-tenant ghost sweep (nexus-ft04v.3), which drops registry rows no data uses, on a request that registers nothing.
        get(TOKEN_A, TENANT_A, "/v1/catalog/collections/list");
        get(TOKEN_B, TENANT_B, "/v1/catalog/collections/list");
        seed();
    }

    @AfterAll
    void stop() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    // ---- two tenants, a different number of everything ---------------------------------------------------------------------

    private static String name(String type, String tag) {
        return type + "__" + tag + "__" + MODEL + "__v1";
    }

    private void tombstone(String tenant, String collection, String supersededBy, boolean selfId) {
        PgContainerHelper.insertCollection(ctx, tenant, collection);
        ctx.update(CATALOG_COLLECTIONS).set(SUPERSEDED_BY, supersededBy)
            .set(SUPERSEDED_BY_ID, selfId ? COLLECTION_ID : DSL.val((Long) null, Long.class))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(NAME.eq(collection)).execute();
    }

    private void document(String tenant, String tumbler, String physicalCollection) {
        PgContainerHelper.insertCatalogDocument(ctx, tenant, tumbler);
        ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, physicalCollection)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))).execute();
    }

    /**
     * A: 2 live collections, 1 dangling docs tombstone; 2 orphan and 1 unstamped catalog_documents row.
     * B: 1 live collection, 2 dangling code tombstones and 1 docs tombstone the stamp resolved to its own id; 1 orphan and 3 unstamped rows.
     */
    private void seed() {
        P31CheckReplay.lift(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        P31CheckReplay.lift(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
        try {
            PgContainerHelper.insertCollection(ctx, TENANT_A, name("code", "ra1"));
            PgContainerHelper.insertCollection(ctx, TENANT_A, name("code", "ra2"));
            tombstone(TENANT_A, name("docs", "ta1"), "gone-a-docs", false);
            document(TENANT_A, "9.1.1", "ghost-a-1");
            document(TENANT_A, "9.1.2", "ghost-a-2");
            document(TENANT_A, "9.1.3", name("code", "ra1"));
            PgContainerHelper.insertCollection(ctx, TENANT_B, name("code", "rb1"));
            tombstone(TENANT_B, name("code", "tb1"), "gone-b-code-1", false);
            tombstone(TENANT_B, name("code", "tb2"), "gone-b-code-2", false);
            tombstone(TENANT_B, name("docs", "tb3"), "gone-b-docs", true);
            document(TENANT_B, "9.2.1", "ghost-b-1");
            document(TENANT_B, "9.2.2", name("code", "rb1"));
            document(TENANT_B, "9.2.3", name("code", "rb1"));
            document(TENANT_B, "9.2.4", name("code", "rb1"));
        } finally {
            P31CheckReplay.restore(ctx, P31CheckReplay.CATALOG_DOCUMENTS);
            P31CheckReplay.restore(ctx, P31CheckReplay.COLLECTIONS_SUPERSEDED);
        }
        // the stamp of the documents table (registered by the walk) is done and its CHECK is NOT VALID: the state that says "VALIDATE waits"
        ctx.update(MOVES).set(DSL.field(DSL.name("completed_at"), SQLDataType.TIMESTAMPWITHTIMEZONE), OffsetDateTime.now())
            .where(DSL.field(DSL.name("label"), String.class).eq(StampLabels.labelFor("catalog_documents"))).execute();
    }

    private long registryRows(String tenant) {
        return ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(tenant));
    }

    // ---- the unauthenticated body ------------------------------------------------------------------------------------------

    private static final class Live implements CompletionBootWork.Reader {
        private final DSLContext ctx;

        Live(DSLContext ctx) {
            this.ctx = ctx;
        }

        @Override
        public CompletionCheck.Result read() {
            return CompletionCheck.evaluate(ctx, CompletionCheck.ReadSet.p31(), CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
        }

        @Override
        public void abort() { }

        @Override
        public void close() { }
    }

    /** The real {@code /v1/status} body, as an unauthenticated caller gets it: no token, no tenant header. */
    private JsonNode statusField() throws Exception {
        var registry = new BootWorkRegistry(Clock.fixed(Instant.parse("2026-11-21T12:00:00Z"), ZoneOffset.UTC));
        new CompletionBootWork(registry, new Live(ctx)).publish();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/status", new StatusHandler(null, null).bootWork(registry::snapshot).rdr229Completion(registry::rdr229Completion));
        server.start();
        try {
            HttpResponse<String> resp = http.send(TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/status").GET().build(),
                HttpResponse.BodyHandlers.ofString());
            assertThat(resp.statusCode()).isEqualTo(200);
            JsonNode field = MAPPER.readTree(resp.body()).get("rdr229_completion");
            assertThat(field).as("the engine reported the field").isNotNull();
            return field;
        } finally {
            server.stop(0);
        }
    }

    @Test
    void theUnauthenticatedStatusBodyCarriesNoTenantCount_withTwoTenantsOnTheEngine() throws Exception {
        assertThat(registryRows(TENANT_A)).as("non-vacuity: two tenants with data").isPositive();
        assertThat(registryRows(TENANT_B)).isPositive().isNotEqualTo(registryRows(TENANT_A));

        JsonNode field = statusField();

        assertThat(field.get("validate_waits")).as("non-vacuity: the state that used to carry orphan and unstamped counts is there")
            .anySatisfy(w -> assertThat(w.get("table").asText()).isEqualTo("catalog_documents"));
        assertThat(keys(field)).as("only the gate bookkeeping and operator text: no registry_rows, dangling, self_id or superseded_by_waiting")
            .isSubsetOf(STATUS_KEYS);
        walk(field, "rdr229_completion");
        assertThat(field.toString()).as("no tenant's name or id either").doesNotContain(TENANT_A).doesNotContain(TENANT_B).doesNotContain("ghost-")
            .doesNotContain("gone-");
    }

    private static List<String> keys(JsonNode node) {
        var out = new java.util.ArrayList<String>();
        node.fieldNames().forEachRemaining(out::add);
        return out;
    }

    /** No numeric value under a key that is not engine bookkeeping, and no text that puts a number in front of what a tenant holds. */
    private static void walk(JsonNode node, String path) {
        if (node.isObject()) {
            node.fields().forEachRemaining(e -> {
                if (e.getValue().isNumber()) {
                    assertThat(NUMERIC_STATUS_KEYS).as("numeric key %s.%s describes tenants", path, e.getKey()).contains(e.getKey());
                }
                walk(e.getValue(), path + "." + e.getKey());
            });
        } else if (node.isArray()) {
            node.forEach(n -> walk(n, path + "[]"));
        } else if (node.isTextual()) {
            assertThat(TENANT_COUNT_TEXT.matcher(node.asText()).find()).as("%s carries a tenant count in its text: %s", path, node.asText()).isFalse();
        }
    }

    // ---- the authenticated route, one tenant at a time ---------------------------------------------------------------------

    private HttpResponse<String> get(String token, String tenant, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token).header("X-Nexus-Tenant", tenant).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private Map<String, Object> counts(String token, String tenant) throws Exception {
        var resp = get(token, tenant, "/v1/catalog/rdr229_completion_counts");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        return MAPPER.readValue(resp.body(), new TypeReference<>() { });
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> table(Map<String, Object> body, String table) {
        return ((List<Map<String, Object>>) body.get("tables")).stream().filter(t -> table.equals(t.get("table"))).findFirst().orElseThrow();
    }

    @Test
    void eachTenantReadsOnlyItsOwnCounts_neverTheOthersNorATotal() throws Exception {
        var a = counts(TOKEN_A, TENANT_A);
        var b = counts(TOKEN_B, TENANT_B);

        assertThat(a.get("registry_rows")).as("A's registry rows, not A + B").isEqualTo((int) registryRows(TENANT_A));
        assertThat(b.get("registry_rows")).isEqualTo((int) registryRows(TENANT_B));
        assertThat(a.get("live_collections")).as("A: %s", a).isEqualTo(2);
        assertThat(b.get("live_collections")).isEqualTo(1);

        var danglingA = map(a.get("dangling_tombstones"));
        assertThat(danglingA.get("total")).isEqualTo(1);
        assertThat(map(danglingA.get("by_content_type"))).containsExactlyEntriesOf(Map.of("docs", 1));
        assertThat(map(map(danglingA.get("self_id")).get("by_content_type"))).isEmpty();
        var danglingB = map(b.get("dangling_tombstones"));
        assertThat(danglingB.get("total")).as("B's two code tombstones and its docs one the stamp resolved to itself").isEqualTo(3);
        assertThat(map(danglingB.get("by_content_type"))).containsEntry("code", 2).containsEntry("docs", 1);
        assertThat(map(danglingB.get("self_id")).get("total")).isEqualTo(1);
        assertThat(map(map(danglingB.get("self_id")).get("by_content_type"))).containsExactlyEntriesOf(Map.of("docs", 1));

        assertThat(table(a, "catalog_documents")).containsEntry("orphans", 2).containsEntry("unstamped", 1);
        assertThat(table(b, "catalog_documents")).containsEntry("orphans", 1).containsEntry("unstamped", 3);
        assertThat(a.toString()).doesNotContain(TENANT_B).doesNotContain("ghost-b").doesNotContain("gone-b");
        assertThat(b.toString()).doesNotContain(TENANT_A).doesNotContain("ghost-a").doesNotContain("gone-a");
    }

    @Test
    void theRouteIsAuthenticated_andReadOnly() throws Exception {
        var anonymous = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/rdr229_completion_counts").GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(anonymous.statusCode()).as("no token, no counts").isIn(401, 403);
        var post = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/rdr229_completion_counts")
            .header("Authorization", "Bearer " + TOKEN_A).header("X-Nexus-Tenant", TENANT_A)
            .POST(java.net.http.HttpRequest.BodyPublishers.ofString("{}")).build(), HttpResponse.BodyHandlers.ofString());
        assertThat(post.statusCode()).isEqualTo(405);
    }
}
